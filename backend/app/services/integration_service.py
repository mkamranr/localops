"""Integrations (spec §10.8): CRUD, credentials, connection tests, discovery runs, host keys.

Credentials are stored as one Fernet-encrypted JSON document and never returned; only
the *names* of the stored fields are exposed so the UI can show what is configured.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.database.session import tenant_session
from app.discovery.engine import load_credentials, load_pins, record_host_key
from app.graphs.dispatch import InvestigationDispatcher
from app.integrations import get_collector_factory, uses_ssh
from app.integrations.base import CollectorError, Target, TestOutcome
from app.models import DiscoveryRun, Integration, Resource, SshHostKey
from app.models.inventory import TOOL_INTEGRATION_TYPES
from app.schemas.common import PageParams
from app.schemas.inventory import (
    IntegrationCreate,
    IntegrationOut,
    IntegrationSettings,
    IntegrationTestResult,
    IntegrationUpdate,
    TargetIn,
    check_type_rules,
)
from app.services import audit_service as audit
from app.services.context import ClientInfo, Principal, RequestContext
from app.tools import catalogue as tool_catalogue

ACTIVE_RUN_WINDOW = timedelta(hours=1)
_CRED_FIELDS_KEY = "credential_fields"


def _now() -> datetime:
    return datetime.now(UTC)


def to_out(i: Integration, resource_count: int = 0) -> IntegrationOut:
    config = dict(i.configuration)
    fields = config.pop(_CRED_FIELDS_KEY, [])
    out = IntegrationOut.model_validate(i)
    out.configuration = config
    out.has_credentials = bool(i.encrypted_credentials)
    out.credential_fields = list(fields)
    out.resource_count = resource_count
    return out


async def resource_counts(session: AsyncSession) -> dict[uuid.UUID, int]:
    rows = await session.execute(
        select(Resource.integration_id, func.count())
        .where(Resource.integration_id.is_not(None))
        .group_by(Resource.integration_id)
    )
    return {r[0]: r[1] for r in rows.all() if r[0] is not None}


async def list_integrations(session: AsyncSession) -> list[IntegrationOut]:
    counts = await resource_counts(session)
    rows = await session.scalars(select(Integration).order_by(Integration.name))
    return [to_out(i, counts.get(i.id, 0)) for i in rows]


async def get_integration(session: AsyncSession, integration_id: uuid.UUID) -> Integration:
    integration = await session.get(Integration, integration_id)
    if integration is None:
        raise NotFoundError("Integration not found")
    return integration


async def integration_out(session: AsyncSession, integration: Integration) -> IntegrationOut:
    count = await session.scalar(
        select(func.count()).select_from(Resource).where(Resource.integration_id == integration.id)
    )
    return to_out(integration, int(count or 0))


async def _name_taken(s: AsyncSession, name: str, exclude: uuid.UUID | None = None) -> bool:
    q = (
        select(func.count())
        .select_from(Integration)
        .where(func.lower(Integration.name) == name.lower())
    )
    if exclude:
        q = q.where(Integration.id != exclude)
    return bool(await s.scalar(q))


def _targets(targets: list[TargetIn]) -> list[dict[str, Any]]:
    seen: set[tuple[str, int | None]] = set()
    out = []
    for t in targets:
        key = (t.host.lower(), t.port)
        if key in seen:
            continue
        seen.add(key)
        out.append(t.model_dump(exclude_none=True))
    return out


def _encrypt(settings: Settings, secrets: dict[str, str]) -> str | None:
    return encrypt_secret(settings, json.dumps(secrets)) if secrets else None


async def create_integration(
    ctx: RequestContext, settings: Settings, data: IntegrationCreate
) -> Integration:
    s, p = ctx.session, ctx.principal
    if await _name_taken(s, data.name):
        raise ConflictError(
            "An integration with this name already exists", details={"field": "name"}
        )
    secrets = data.credentials.plain() if data.credentials else {}
    config = data.configuration.model_dump(exclude_none=True)
    config[_CRED_FIELDS_KEY] = sorted(secrets)
    integration = Integration(
        tenant_id=p.tenant_id,
        name=data.name.strip(),
        integration_type=data.integration_type,
        description=data.description,
        configuration=config,
        encrypted_credentials=_encrypt(settings, secrets),
        targets=_targets(data.targets),
        enabled=data.enabled,
        schedule_minutes=data.schedule_minutes,
        next_run_at=_now() if data.schedule_minutes else None,
    )
    s.add(integration)
    await s.flush()
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="integration.create",
        entity_type="integration",
        entity_id=integration.id,
        details={
            "name": integration.name,
            "type": integration.integration_type,
            "targets": len(integration.targets),
            "credential_fields": sorted(secrets),
            "schedule_minutes": integration.schedule_minutes,
        },
        client=ctx.client,
    )
    return integration


async def update_integration(
    ctx: RequestContext, settings: Settings, integration_id: uuid.UUID, data: IntegrationUpdate
) -> Integration:
    s, p = ctx.session, ctx.principal
    integration = await get_integration(s, integration_id)
    changes: dict[str, Any] = {}
    if data.name is not None and data.name.strip() != integration.name:
        if await _name_taken(s, data.name, exclude=integration.id):
            raise ConflictError(
                "An integration with this name already exists", details={"field": "name"}
            )
        integration.name = changes["name"] = data.name.strip()
    if data.description is not None and data.description != integration.description:
        integration.description = changes["description"] = data.description
    fields = list(integration.configuration.get(_CRED_FIELDS_KEY, []))
    if data.configuration is not None or data.targets is not None:
        settings_model = data.configuration or IntegrationSettings.model_validate(
            {k: v for k, v in integration.configuration.items() if k != _CRED_FIELDS_KEY}
        )
        targets = (
            data.targets
            if data.targets is not None
            else [TargetIn.model_validate(t) for t in integration.targets]
        )
        try:
            check_type_rules(integration.integration_type, settings_model, targets)
        except ValueError as exc:
            raise ValidationFailedError(str(exc)) from exc
        if data.configuration is not None:
            config = data.configuration.model_dump(exclude_none=True)
            config[_CRED_FIELDS_KEY] = fields
            if config != integration.configuration:
                integration.configuration = config
                changes["configuration"] = {
                    k: v for k, v in config.items() if k != _CRED_FIELDS_KEY
                }
        if data.targets is not None:
            new_targets = _targets(data.targets)
            if new_targets != integration.targets:
                integration.targets = new_targets
                changes["targets"] = len(new_targets)
    if data.clear_credentials and integration.encrypted_credentials:
        integration.encrypted_credentials = None
        fields = []
        changes["credentials"] = "cleared"
    elif data.credentials is not None and data.credentials.plain():
        current = (
            json.loads(decrypt_secret(settings, integration.encrypted_credentials))
            if integration.encrypted_credentials
            else {}
        )
        current.update(data.credentials.plain())
        integration.encrypted_credentials = _encrypt(settings, current)
        fields = sorted(current)
        changes["credentials"] = {"updated_fields": sorted(data.credentials.plain())}
    if fields != integration.configuration.get(_CRED_FIELDS_KEY, []):
        integration.configuration = {**integration.configuration, _CRED_FIELDS_KEY: fields}
    if data.enabled is not None and data.enabled != integration.enabled:
        integration.enabled = changes["enabled"] = data.enabled
    if data.clear_schedule and integration.schedule_minutes is not None:
        integration.schedule_minutes = None
        integration.next_run_at = None
        changes["schedule_minutes"] = None
    elif (
        data.schedule_minutes is not None and data.schedule_minutes != integration.schedule_minutes
    ):
        integration.schedule_minutes = changes["schedule_minutes"] = data.schedule_minutes
        integration.next_run_at = _now()
    await s.flush()
    if changes and integration.integration_type in TOOL_INTEGRATION_TYPES:
        tool_catalogue.invalidate(p.tenant_id)
    if changes:
        await audit.record(
            s,
            tenant_id=p.tenant_id,
            actor=p,
            action="integration.update",
            entity_type="integration",
            entity_id=integration.id,
            details={"name": integration.name, "changes": changes},
            client=ctx.client,
        )
    return integration


async def delete_integration(ctx: RequestContext, integration_id: uuid.UUID) -> None:
    s, p = ctx.session, ctx.principal
    integration = await get_integration(s, integration_id)
    # Resources stay in the inventory (with their history); they just lose their source.
    await s.execute(
        update(Resource)
        .where(Resource.integration_id == integration.id)
        .values(integration_id=None)
    )
    await s.delete(integration)
    await s.flush()
    tool_catalogue.invalidate(p.tenant_id)
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="integration.delete",
        entity_type="integration",
        entity_id=integration_id,
        details={"name": integration.name, "type": integration.integration_type},
        client=ctx.client,
    )


# ------------------------------------------------------------------ test


async def test_integration(
    principal: Principal,
    client: ClientInfo,
    settings: Settings,
    integration_id: uuid.UUID,
    target_in: TargetIn | None,
) -> IntegrationTestResult:
    """Connect to one target with the stored credentials. Pins the SSH host key on
    first contact (trust on first use). No DB connection is held during the probe."""
    async with tenant_session(principal.tenant_id) as s:
        integration = await get_integration(s, integration_id)
        if integration.integration_type in TOOL_INTEGRATION_TYPES:
            itype = integration.integration_type
            config = dict(integration.configuration)
            credentials = load_credentials(settings, integration)
            name = integration.name
            tool_probe = True
        else:
            tool_probe = False
    if tool_probe:
        outcome = await probe_tool_integration(settings, itype, config, credentials)
        async with tenant_session(principal.tenant_id) as s:
            await audit.record(
                s,
                tenant_id=principal.tenant_id,
                actor=principal,
                action="integration.test",
                outcome="success" if outcome.ok else "failure",
                entity_type="integration",
                entity_id=integration_id,
                details={
                    "name": name,
                    "target": config.get("url"),
                    "message": outcome.message[:300],
                },
                client=client,
            )
        return IntegrationTestResult(
            ok=outcome.ok,
            target=str(config.get("url") or ""),
            message=outcome.message,
            details=dict(outcome.details),
        )
    async with tenant_session(principal.tenant_id) as s:
        integration = await get_integration(s, integration_id)
        if target_in is not None:
            target = Target.from_dict(target_in.model_dump())
        elif integration.targets:
            target = Target.from_dict(integration.targets[0])
        else:
            raise ValidationFailedError("Add at least one target first")
        credentials = load_credentials(settings, integration)
        pins = await load_pins(s, integration, [target])
        itype, config = integration.integration_type, dict(integration.configuration)
        name = integration.name
    try:
        collector = get_collector_factory()(itype, config, credentials, pins)
        outcome = await collector.test(target)
    except CollectorError as exc:
        outcome = TestOutcome(False, str(exc), {"code": exc.code})
    details = dict(outcome.details)
    async with tenant_session(principal.tenant_id) as s:
        if outcome.host_key is not None and uses_ssh(itype, config):
            details["host_key_pinned_now"] = await record_host_key(
                s, principal.tenant_id, outcome.host_key
            )
        await audit.record(
            s,
            tenant_id=principal.tenant_id,
            actor=principal,
            action="integration.test",
            outcome="success" if outcome.ok else "failure",
            entity_type="integration",
            entity_id=integration_id,
            details={"name": name, "target": target.label, "message": outcome.message[:300]},
            client=client,
        )
    return IntegrationTestResult(
        ok=outcome.ok, target=target.label, message=outcome.message, details=details
    )


# ------------------------------------------------------------------ discovery runs


async def active_run(session: AsyncSession, integration_id: uuid.UUID) -> DiscoveryRun | None:
    return await session.scalar(
        select(DiscoveryRun)
        .where(
            DiscoveryRun.integration_id == integration_id,
            DiscoveryRun.status.in_(("queued", "running")),
            DiscoveryRun.started_at > _now() - ACTIVE_RUN_WINDOW,
        )
        .limit(1)
    )


async def create_run(
    session: AsyncSession,
    integration: Integration,
    *,
    trigger: str,
    triggered_by: uuid.UUID | None,
) -> DiscoveryRun:
    run = DiscoveryRun(
        tenant_id=integration.tenant_id,
        integration_id=integration.id,
        trigger=trigger,
        triggered_by=triggered_by,
        status="queued",
        started_at=_now(),
    )
    session.add(run)
    await session.flush()
    return run


async def start_discovery(
    principal: Principal,
    client: ClientInfo,
    integration_id: uuid.UUID,
    dispatcher: InvestigationDispatcher,
) -> DiscoveryRun:
    async with tenant_session(principal.tenant_id) as s:
        integration = await get_integration(s, integration_id)
        if integration.integration_type in TOOL_INTEGRATION_TYPES:
            raise ValidationFailedError(
                "Tool integrations do not discover resources; use 'Sync tools' instead"
            )
        if not integration.enabled:
            raise ConflictError("Integration is disabled")
        if not integration.targets:
            raise ValidationFailedError("Add at least one target first")
        if await active_run(s, integration.id):
            raise ConflictError("A discovery run for this integration is already in progress")
        run = await create_run(s, integration, trigger="manual", triggered_by=principal.user_id)
        await audit.record(
            s,
            tenant_id=principal.tenant_id,
            actor=principal,
            action="integration.discover_requested",
            entity_type="integration",
            entity_id=integration.id,
            details={"name": integration.name, "run_id": str(run.id)},
            client=client,
        )
    # Committed before the worker can pick it up.
    await dispatcher.enqueue_discovery(principal.tenant_id, run.id)
    return run


async def list_runs(
    session: AsyncSession, integration_id: uuid.UUID, page: PageParams
) -> tuple[list[DiscoveryRun], int]:
    await get_integration(session, integration_id)
    base = select(DiscoveryRun).where(DiscoveryRun.integration_id == integration_id)
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = await session.scalars(
        base.order_by(DiscoveryRun.started_at.desc()).offset(page.offset).limit(page.page_size)
    )
    return list(rows), int(total or 0)


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> DiscoveryRun:
    run = await session.get(DiscoveryRun, run_id)
    if run is None:
        raise NotFoundError("Discovery run not found")
    return run


# ------------------------------------------------------------------ host keys


async def list_host_keys(session: AsyncSession, integration_id: uuid.UUID) -> list[SshHostKey]:
    integration = await get_integration(session, integration_id)
    if not uses_ssh(integration.integration_type, integration.configuration):
        return []
    default_port = int(integration.configuration.get("port") or 22)
    wanted = {(str(t["host"]), int(t.get("port") or default_port)) for t in integration.targets}
    if not wanted:
        return []
    rows = await session.scalars(
        select(SshHostKey)
        .where(SshHostKey.host.in_({h for h, _ in wanted}))
        .order_by(SshHostKey.host)
    )
    return [r for r in rows if (r.host, r.port) in wanted]


async def reset_host_key(ctx: RequestContext, integration_id: uuid.UUID, key_id: uuid.UUID) -> None:
    s, p = ctx.session, ctx.principal
    keys = {k.id: k for k in await list_host_keys(s, integration_id)}
    key = keys.get(key_id)
    if key is None:
        raise NotFoundError("Host key not found")
    await s.delete(key)
    await s.flush()
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="integration.host_key_reset",
        entity_type="integration",
        entity_id=integration_id,
        details={"host": key.host, "port": key.port, "fingerprint": key.fingerprint},
        client=ctx.client,
    )


# ------------------------------------------------------------------ tool integrations


async def probe_tool_integration(
    settings: Settings, itype: str, config: dict[str, Any], credentials: dict[str, Any]
) -> TestOutcome:
    """MCP: initialize + tools/list. HTTP API: one GET of the base URL (any status
    below 500 proves reachability and TLS; the status is reported)."""
    import httpx

    from app.integrations import http_api
    from app.integrations.http_api import auth_headers, verify_option
    from app.integrations.mcp import McpClient, McpError, config_from_integration

    if itype == "mcp":
        try:
            async with McpClient(
                config_from_integration(config, credentials, settings.mcp_default_timeout_seconds)
            ) as mcp:
                tools = await mcp.list_tools()
                server = mcp.server_info
        except McpError as exc:
            return TestOutcome(False, str(exc), {"code": exc.code})
        label = " ".join(str(server.get(k, "")) for k in ("name", "version")).strip() or "server"
        return TestOutcome(
            True,
            f"Connected to {label}; {len(tools)} tool(s) offered",
            {"server": server, "tools": [t.name for t in tools[:50]]},
        )
    url = str(config.get("url") or "")
    try:
        async with httpx.AsyncClient(
            transport=http_api._transport_override,
            timeout=float(config.get("timeout_seconds") or 20),
            verify=verify_option(config, credentials),
            follow_redirects=False,
            trust_env=False,
        ) as http:
            resp = await http.get(url, headers=auth_headers(config, credentials))
    except httpx.HTTPError as exc:
        return TestOutcome(False, f"Cannot reach {url}: {type(exc).__name__}", {})
    ok = resp.status_code < 500
    return TestOutcome(ok, f"{url} answered HTTP {resp.status_code}", {"status": resp.status_code})
