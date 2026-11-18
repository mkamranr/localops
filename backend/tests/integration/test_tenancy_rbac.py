"""Tenant isolation, RBAC and audit behaviour through the API."""

from __future__ import annotations

from typing import Any

from tests.integration.conftest import STRONG, LoginFn, Session, role_id


async def _create_user(admin: Session, username: str, role: str | None = None) -> dict[str, Any]:
    role_ids = [await role_id(admin, role)] if role else []
    r = await admin.post(
        "/api/v1/users",
        json={
            "username": username,
            "email": f"{username}@example.test",
            "password": STRONG,
            "role_ids": role_ids,
        },
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


async def test_tenant_creation_seeds_roles_and_admin(make_tenant: Any) -> None:
    t = await make_tenant()
    me = (await t.admin.get("/api/v1/auth/me")).json()
    assert me["roles"] == ["Tenant Administrator"]
    assert "tenant.manage" not in me["permissions"]
    roles = {r["name"] for r in (await t.admin.get("/api/v1/roles")).json()}
    assert "Super Administrator" not in roles
    assert len(roles) == 6


async def test_cross_tenant_access_is_impossible(make_tenant: Any) -> None:
    a, b = await make_tenant(), await make_tenant()
    alice = await _create_user(a.admin, "alice", "Operator")

    # B lists only its own users.
    names = {u["username"] for u in (await b.admin.get("/api/v1/users")).json()["items"]}
    assert names == {"tadmin"}
    # Direct access by id across tenants looks like "not found".
    assert (await b.admin.get(f"/api/v1/users/{alice['id']}")).status_code == 404
    r = await b.admin.put(f"/api/v1/users/{alice['id']}", json={"display_name": "pwned"})
    assert r.status_code == 404
    assert (await b.admin.delete(f"/api/v1/users/{alice['id']}")).status_code == 404
    # Roles from tenant A can't be assigned in tenant B.
    a_role = await role_id(a.admin, "Operator")
    b_user = await _create_user(b.admin, "bob")
    r = await b.admin.put(f"/api/v1/users/{b_user['id']}/roles", json={"role_ids": [a_role]})
    assert r.status_code == 404
    # Audit logs are tenant-scoped.
    actions = {i["action"] for i in (await b.admin.get("/api/v1/audit-logs")).json()["items"]}
    alice_logs = await b.admin.get("/api/v1/audit-logs", params={"entity_id": alice["id"]})
    assert alice_logs.json()["total"] == 0
    assert "user.create" in actions


async def test_tenant_admin_cannot_manage_tenants(make_tenant: Any) -> None:
    t = await make_tenant()
    r = await t.admin.get("/api/v1/tenants")
    assert r.status_code == 403
    assert r.json()["error"]["details"]["missing_permissions"] == ["tenant.manage"]


async def test_read_only_user_is_forbidden_from_mutations(make_tenant: Any, login: LoginFn) -> None:
    t = await make_tenant()
    await _create_user(t.admin, "viewer", "Read Only")
    viewer = await login(t.slug, "viewer", STRONG)
    assert (await viewer.get("/api/v1/dashboard/summary")).status_code == 200
    assert (await viewer.get("/api/v1/users")).status_code == 403
    assert (await viewer.get("/api/v1/audit-logs")).status_code == 403
    r = await viewer.post(
        "/api/v1/users", json={"username": "x" * 5, "email": "x@y.zz", "password": STRONG}
    )
    assert r.status_code == 403


async def test_privilege_escalation_blocked(
    platform_admin: Session, login: LoginFn, make_tenant: Any
) -> None:
    # A Tenant Administrator in the system tenant cannot hand out Super Administrator.
    ta = await _create_user(platform_admin, "sysadmin2", "Tenant Administrator")
    victim = await _create_user(platform_admin, "victim")
    ta_session = await login("system", ta["username"], STRONG)
    super_role = await role_id(platform_admin, "Super Administrator")
    r = await ta_session.put(f"/api/v1/users/{victim['id']}/roles", json={"role_ids": [super_role]})
    assert r.status_code == 403
    assert "tenant.manage" in r.json()["error"]["details"]["permissions"]

    # Nobody can change their own roles.
    t = await make_tenant()
    me = (await t.admin.get("/api/v1/auth/me")).json()
    r = await t.admin.put(f"/api/v1/users/{me['id']}/roles", json={"role_ids": []})
    assert r.status_code == 403


async def test_disable_and_delete_user(make_tenant: Any, login: LoginFn) -> None:
    t = await make_tenant()
    u = await _create_user(t.admin, "temp", "Operator")
    s = await login(t.slug, "temp", STRONG)
    r = await t.admin.put(f"/api/v1/users/{u['id']}", json={"status": "disabled"})
    assert r.status_code == 200
    assert (await s.get("/api/v1/auth/me")).json()["error"]["code"] == "account_inactive"
    assert (await s.client.post("/api/v1/auth/refresh")).status_code == 401

    assert (await t.admin.delete(f"/api/v1/users/{u['id']}")).status_code == 204
    assert (await t.admin.get(f"/api/v1/users/{u['id']}")).status_code == 404
    # Username stays reserved after deletion.
    r = await t.admin.post(
        "/api/v1/users", json={"username": "temp", "email": "new@x.test", "password": STRONG}
    )
    assert r.status_code == 409


async def test_duplicate_user_conflict(make_tenant: Any) -> None:
    t = await make_tenant()
    await _create_user(t.admin, "dupe")
    r = await t.admin.post(
        "/api/v1/users",
        json={"username": "DUPE", "email": "other@x.test", "password": STRONG},
    )
    assert r.status_code == 409
    assert r.json()["error"]["details"]["field"] == "username"


async def test_disabled_tenant_locks_out_users(
    platform_admin: Session, make_tenant: Any, client: Any
) -> None:
    t = await make_tenant()
    assert (await platform_admin.post(f"/api/v1/tenants/{t.id}/disable")).status_code == 200
    assert (await t.admin.get("/api/v1/auth/me")).status_code == 401
    r = await client.post(
        "/api/v1/auth/login", json={"tenant": t.slug, "username": "tadmin", "password": STRONG}
    )
    assert r.status_code == 401
    assert (await platform_admin.post(f"/api/v1/tenants/{t.id}/enable")).status_code == 200


async def test_system_tenant_cannot_be_disabled(platform_admin: Session) -> None:
    tenants = (await platform_admin.get("/api/v1/tenants")).json()["items"]
    system = next(t for t in tenants if t["slug"] == "system")
    assert (await platform_admin.post(f"/api/v1/tenants/{system['id']}/disable")).status_code == 403


async def test_duplicate_tenant_slug(platform_admin: Session) -> None:
    body = {
        "name": "Dup",
        "slug": "default",
        "admin": {"username": "admin", "email": "a@b.test", "password": STRONG},
    }
    assert (await platform_admin.post("/api/v1/tenants", json=body)).status_code == 409


async def test_dashboard_summary(platform_admin: Session) -> None:
    r = await platform_admin.get("/api/v1/dashboard/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["tenant"] == "system"
    assert body["users"]["active"] >= 1
    assert body["activity"]["logins_24h"] >= 1
    assert {s["name"] for s in body["services"]} == {"postgres", "redis", "qdrant"}
    assert (await platform_admin.get("/api/v1/dashboard/activity")).status_code == 200


async def test_permissions_catalogue(platform_admin: Session) -> None:
    perms = (await platform_admin.get("/api/v1/permissions")).json()
    assert "audit.read" in {p["code"] for p in perms}
