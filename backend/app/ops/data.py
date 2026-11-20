"""Database-level operational checks and exports (run through the CLI)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app import __version__
from app.core.config import Settings
from app.core.crypto import EncryptionUnavailableError, decrypt_secret
from app.database.base import Base
from app.database.session import tenant_session
from app.models import Integration, ModelEndpoint, Tenant

# Never exported: session tokens, and bulky data that is derived or regenerated.
EXPORT_SKIP_TABLES = {"refresh_tokens", "knowledge_chunks", "resource_metric_samples"}
SENSITIVE_COLUMNS = {"password_hash", "token_hash"}


@dataclass
class SecretsReport:
    checked: int = 0
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


async def _tenants() -> list[tuple[uuid.UUID, str]]:
    async with tenant_session(None, platform_admin=True) as session:
        rows = await session.execute(select(Tenant.id, Tenant.slug).order_by(Tenant.slug))
        return [(r[0], r[1]) for r in rows]


async def check_secrets(settings: Settings) -> SecretsReport:
    """Try to decrypt every stored credential with the configured encryption key.

    Used after a restore: a wrong LOCALOPS_ENCRYPTION_KEY makes every stored model API
    key and integration credential unusable, so restore verifies it explicitly.
    """
    report = SecretsReport()
    for tenant_id, slug in await _tenants():
        async with tenant_session(tenant_id) as session:
            rows: list[tuple[str, str, str | None]] = []
            for m in await session.execute(
                select(ModelEndpoint.name, ModelEndpoint.encrypted_api_key)
            ):
                rows.append(("model endpoint", m[0], m[1]))
            for i in await session.execute(
                select(Integration.name, Integration.encrypted_credentials)
            ):
                rows.append(("integration", i[0], i[1]))
        for kind, name, ciphertext in rows:
            if not ciphertext:
                continue
            report.checked += 1
            try:
                decrypt_secret(settings, ciphertext)
            except EncryptionUnavailableError:
                report.failed.append(f"{slug}: {kind} {name!r}")
    return report


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes):
        return None
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return str(value)  # UUID, datetime, Decimal, IP addresses, ...


def _exportable(column: str) -> bool:
    return not column.startswith("encrypted_") and column not in SENSITIVE_COLUMNS


async def export_tenant(slug: str) -> dict[str, Any]:
    """One tenant's data as JSON-ready dicts, without credentials or password hashes.

    Reads run as the runtime role under the tenant's row-level-security context, so
    nothing from another tenant can appear in the export.
    """
    match = [t for t in await _tenants() if t[1] == slug]
    if not match:
        raise LookupError(f"tenant {slug!r} not found")
    tenant_id = match[0][0]
    tables: dict[str, list[dict[str, Any]]] = {}
    async with tenant_session(tenant_id) as session:
        cols = (
            Tenant.id,
            Tenant.name,
            Tenant.slug,
            Tenant.status,
            Tenant.settings,
            Tenant.created_at,
        )
        tenant = (
            (await session.execute(select(*cols).where(Tenant.id == tenant_id))).mappings().one()
        )
        for table in Base.metadata.sorted_tables:
            if table.name in EXPORT_SKIP_TABLES or "tenant_id" not in table.c:
                continue
            columns = [c for c in table.c if _exportable(c.name)]
            order = [table.c[k] for k in ("created_at", "id") if k in table.c]
            stmt = select(*columns).where(table.c.tenant_id == tenant_id).order_by(*order)
            result = await session.execute(stmt)
            tables[table.name] = [_jsonable(dict(r)) for r in result.mappings()]
    return {
        "format": "localops-tenant-export",
        "version": 1,
        "localops_version": __version__,
        "exported_at": datetime.now(UTC).isoformat(),
        "tenant": _jsonable(dict(tenant)),
        "tables": tables,
    }
