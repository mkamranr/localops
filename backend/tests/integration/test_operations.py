"""Phase 7 operations: stored-secret verification after restore, and tenant export."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.core.config import get_settings
from app.ops.data import check_secrets, export_tenant, reset_password
from tests.integration.conftest import LoginFn, TenantInfo

pytestmark = pytest.mark.usefixtures("bootstrapped")

MakeTenant = Callable[[], Coroutine[Any, Any, TenantInfo]]


async def _integration(tenant: TenantInfo, password: str) -> dict[str, Any]:
    r = await tenant.admin.post(
        "/api/v1/integrations",
        json={
            "name": "Linux hosts",
            "integration_type": "linux_ssh",
            "configuration": {"username": "ops", "port": 22, "timeout_seconds": 10},
            "credentials": {"password": password},
            "targets": [{"host": "192.0.2.10", "name": "app-01"}],
        },
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


async def test_check_secrets_detects_a_wrong_encryption_key(make_tenant: MakeTenant) -> None:
    tenant = await make_tenant()
    await _integration(tenant, "s3cret-password")
    settings = get_settings()

    good = await check_secrets(settings)
    assert good.ok and good.checked >= 1

    wrong = settings.model_copy(
        update={"encryption_key": SecretStr(Fernet.generate_key().decode())}
    )
    bad = await check_secrets(wrong)
    assert not bad.ok
    assert any(f"{tenant.slug}: integration 'Linux hosts'" == item for item in bad.failed)


async def test_tenant_export_excludes_secrets_and_other_tenants(make_tenant: MakeTenant) -> None:
    alpha, beta = await make_tenant(), await make_tenant()
    await _integration(alpha, "alpha-password-123")
    await _integration(beta, "beta-password-456")

    data = await export_tenant(alpha.slug)
    assert data["format"] == "localops-tenant-export"
    assert data["tenant"]["slug"] == alpha.slug
    tables = data["tables"]
    assert [i["name"] for i in tables["integrations"]] == ["Linux hosts"]
    assert {u["username"] for u in tables["users"]} == {"tadmin"}
    assert "refresh_tokens" not in tables

    blob = json.dumps(data)
    for forbidden in ("alpha-password-123", "beta-password-456", "encrypted_", "password_hash"):
        assert forbidden not in blob
    assert beta.id not in blob and beta.slug not in blob

    with pytest.raises(LookupError):
        await export_tenant("no-such-tenant")


async def test_reset_password_recovers_access(make_tenant: MakeTenant, login: LoginFn) -> None:
    tenant = await make_tenant()
    status = await reset_password(tenant.slug, "TAdmin", "recovered-password-123")
    assert status == "active"

    session = await login(tenant.slug, "tadmin", "recovered-password-123")
    me = (await session.get("/api/v1/auth/me")).json()
    assert me["must_change_password"] is True

    with pytest.raises(LookupError):
        await reset_password(tenant.slug, "nobody", "recovered-password-123")
    with pytest.raises(LookupError):
        await reset_password("no-such-tenant", "tadmin", "recovered-password-123")
