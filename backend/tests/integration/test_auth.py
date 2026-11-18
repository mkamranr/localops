"""Authentication flows against a real database."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest

from app.services import auth_service
from tests.integration.conftest import ADMIN_PASSWORD, STRONG, LoginFn, Session

pytestmark = pytest.mark.usefixtures("bootstrapped")


async def test_login_me_and_audit(platform_admin: Session) -> None:
    me = (await platform_admin.get("/api/v1/auth/me")).json()
    assert me["username"] == "admin"
    assert me["tenant"]["slug"] == "system"
    assert "Super Administrator" in me["roles"]
    assert "tenant.manage" in me["permissions"]
    assert me["must_change_password"] is True

    logs = (await platform_admin.get("/api/v1/audit-logs", params={"action": "auth.login"})).json()
    assert logs["items"][0]["outcome"] == "success"


@pytest.mark.parametrize(
    "creds",
    [
        {"tenant": "system", "username": "admin", "password": "wrong-password"},
        {"tenant": "system", "username": "nobody", "password": ADMIN_PASSWORD},
        {"tenant": "no-such-tenant", "username": "admin", "password": ADMIN_PASSWORD},
        {"tenant": "default", "username": "admin", "password": ADMIN_PASSWORD},
    ],
)
async def test_login_failures_are_uniform(client: httpx.AsyncClient, creds: dict[str, str]) -> None:
    r = await client.post("/api/v1/auth/login", json=creds)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_credentials"
    assert r.json()["error"]["message"] == "Invalid tenant, username or password"


async def test_failed_login_is_audited(client: httpx.AsyncClient, platform_admin: Session) -> None:
    await client.post(
        "/api/v1/auth/login",
        json={"tenant": "system", "username": "admin", "password": "nope-nope"},
    )
    logs = (
        await platform_admin.get(
            "/api/v1/audit-logs", params={"action": "auth.login", "outcome": "failure"}
        )
    ).json()
    assert logs["total"] >= 1
    assert logs["items"][0]["details"]["reason"] == "invalid_credentials"


async def test_login_rate_limited(client: httpx.AsyncClient) -> None:
    body = {"tenant": "system", "username": "admin", "password": "wrong-password"}
    codes = [(await client.post("/api/v1/auth/login", json=body)).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10
    assert codes[10] == 429


async def test_refresh_rotates_and_cookie_is_httponly(login: LoginFn) -> None:
    s = await login("system", "admin", ADMIN_PASSWORD)
    old_cookie = s.client.cookies.get("localops_refresh")
    assert old_cookie
    r = await s.client.post("/api/v1/auth/refresh")
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    new_cookie = s.client.cookies.get("localops_refresh")
    assert new_cookie and new_cookie != old_cookie
    me = await s.client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"}
    )
    assert me.status_code == 200


async def test_refresh_reuse_revokes_family(
    login: LoginFn, monkeypatch: pytest.MonkeyPatch, app: Any
) -> None:
    monkeypatch.setattr(auth_service, "REFRESH_REUSE_GRACE", timedelta(seconds=-1))
    s = await login("system", "admin", ADMIN_PASSWORD)
    stolen = s.client.cookies.get("localops_refresh")
    assert (await s.client.post("/api/v1/auth/refresh")).status_code == 200
    legit = s.client.cookies.get("localops_refresh")

    attacker = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    attacker.cookies.set("localops_refresh", stolen or "", path="/api/v1/auth")
    r = await attacker.post("/api/v1/auth/refresh")
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "Refresh token has been revoked"

    # The legitimate session's token was part of the same family and is now dead too.
    s.client.cookies.set("localops_refresh", legit or "", path="/api/v1/auth")
    assert (await s.client.post("/api/v1/auth/refresh")).status_code == 401


async def test_concurrent_refresh_within_grace_is_allowed(login: LoginFn, app: Any) -> None:
    s = await login("system", "admin", ADMIN_PASSWORD)
    original = s.client.cookies.get("localops_refresh") or ""
    assert (await s.client.post("/api/v1/auth/refresh")).status_code == 200
    other_tab = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    other_tab.cookies.set("localops_refresh", original, path="/api/v1/auth")
    assert (await other_tab.post("/api/v1/auth/refresh")).status_code == 200


async def test_logout_revokes_access_and_refresh(login: LoginFn) -> None:
    s = await login("system", "admin", ADMIN_PASSWORD)
    refresh_cookie = s.client.cookies.get("localops_refresh") or ""
    assert (await s.post("/api/v1/auth/logout")).status_code == 204
    assert (await s.get("/api/v1/auth/me")).json()["error"]["code"] == "token_revoked"
    s.client.cookies.set("localops_refresh", refresh_cookie, path="/api/v1/auth")
    assert (await s.client.post("/api/v1/auth/refresh")).status_code == 401


async def test_change_password(make_tenant: Any, login: LoginFn) -> None:
    t = await make_tenant()
    other_session = await login(t.slug, "tadmin", STRONG)

    bad = await t.admin.post(
        "/api/v1/auth/change-password",
        json={"current_password": "wrong-current", "new_password": "Another-Strong-Pass-2"},
    )
    assert bad.status_code == 401

    r = await t.admin.post(
        "/api/v1/auth/change-password",
        json={"current_password": STRONG, "new_password": "Another-Strong-Pass-2"},
    )
    assert r.status_code == 200
    new = Session(t.admin.client, r.json()["access_token"])
    me = (await new.get("/api/v1/auth/me")).json()
    assert me["must_change_password"] is False

    # The old access token of this session is revoked; other sessions' refresh tokens too.
    assert (await t.admin.get("/api/v1/auth/me")).status_code == 401
    assert (await other_session.client.post("/api/v1/auth/refresh")).status_code == 401
    await login(t.slug, "tadmin", "Another-Strong-Pass-2")

    # Failure was audited even though the request was rejected.
    logs = (await new.get("/api/v1/audit-logs", params={"action": "auth.password_change"})).json()
    assert {i["outcome"] for i in logs["items"]} == {"success", "failure"}
