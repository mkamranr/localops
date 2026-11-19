"""Phase 3 end-to-end: integrations, discovery over real SSH, inventory, graph, RLS."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncssh
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.database.session import tenant_session
from app.discovery.scheduler import schedule_due
from app.graphs.dispatch import InlineDispatcher
from app.models import AuditLog, SshHostKey
from tests.fakes import FakeLLM
from tests.integration.conftest import STRONG, LoginFn, Session, TenantInfo, role_id
from tests.inventory_fixtures import (
    CONTAINERS,
    LINUX_OUTPUT,
    FakeHost,
    port_of,
    start_ssh_server,
)

pytestmark = pytest.mark.usefixtures("bootstrapped")


async def _drain(app: FastAPI) -> None:
    dispatcher: InlineDispatcher = app.state.dispatcher
    await dispatcher.drain()


@pytest.fixture
async def ssh_host() -> AsyncIterator[tuple[FakeHost, int]]:
    host = FakeHost()
    server = await start_ssh_server(host, asyncssh.generate_private_key("ssh-ed25519"))
    yield host, port_of(server)
    server.close()


async def _linux_integration(s: Session, port: int, **extra: Any) -> dict[str, Any]:
    body = {
        "name": "Linux hosts",
        "integration_type": "linux_ssh",
        "configuration": {"username": "ops", "port": port, "timeout_seconds": 10},
        "credentials": {"password": "s3cret"},
        "targets": [{"host": "127.0.0.1", "name": "app-01"}],
        **extra,
    }
    r = await s.post("/api/v1/integrations", json=body)
    assert r.status_code == 201, r.text
    return dict(r.json())


async def _audit_actions(tenant_id: str, entity_id: str) -> set[str]:
    async with tenant_session(uuid.UUID(tenant_id)) as session:
        rows = await session.scalars(
            select(AuditLog.action).where(AuditLog.entity_id == uuid.UUID(entity_id))
        )
        return set(rows)


async def _resources(s: Session, **params: Any) -> dict[str, dict[str, Any]]:
    r = await s.get("/api/v1/resources", params={"page_size": 200, **params})
    assert r.status_code == 200, r.text
    return {x["name"]: x for x in r.json()["items"]}


# ------------------------------------------------------------------ integrations


async def test_integration_crud_and_secret_handling(make_tenant: Any) -> None:
    t: TenantInfo = await make_tenant()
    s = t.admin
    created = await _linux_integration(s, 22)
    assert created["has_credentials"] is True
    assert created["credential_fields"] == ["password"]
    assert created["configuration"]["username"] == "ops"
    assert "credential_fields" not in created["configuration"]

    listing = await s.get("/api/v1/integrations")
    assert "s3cret" not in listing.text and "encrypted" not in listing.text

    iid = created["id"]
    r = await s.put(
        f"/api/v1/integrations/{iid}",
        json={"credentials": {"private_key": "KEY", "passphrase": "pp"}, "schedule_minutes": 15},
    )
    assert r.status_code == 200, r.text
    assert r.json()["credential_fields"] == ["passphrase", "password", "private_key"]
    assert r.json()["schedule_minutes"] == 15 and r.json()["next_run_at"]
    r = await s.put(f"/api/v1/integrations/{iid}", json={"clear_credentials": True})
    assert r.json()["has_credentials"] is False and r.json()["credential_fields"] == []

    dup = await s.post(
        "/api/v1/integrations",
        json={"name": "linux HOSTS", "integration_type": "linux_ssh"},
    )
    assert dup.status_code == 409
    bad_http = await s.post(
        "/api/v1/integrations",
        json={"name": "web", "integration_type": "http", "targets": [{"host": "intranet"}]},
    )
    assert bad_http.status_code == 422
    bad_winrm = await s.post(
        "/api/v1/integrations",
        json={
            "name": "win",
            "integration_type": "windows_winrm",
            "configuration": {"transport": "basic"},
        },
    )
    assert bad_winrm.status_code == 422
    bad_target = await s.post(
        "/api/v1/integrations",
        json={"name": "x", "integration_type": "linux_ssh", "targets": [{"host": "-oProxy=x"}]},
    )
    assert bad_target.status_code == 422

    assert (await s.delete(f"/api/v1/integrations/{iid}")).status_code == 204
    actions = await _audit_actions(t.id, created["id"])
    assert {"integration.create", "integration.update", "integration.delete"} <= actions


# ------------------------------------------------------------------ discovery


async def test_discovery_over_ssh_end_to_end(
    app: FastAPI, make_tenant: Any, ssh_host: tuple[FakeHost, int]
) -> None:
    host, port = ssh_host
    t: TenantInfo = await make_tenant()
    s = t.admin
    integration = await _linux_integration(s, port)
    iid = integration["id"]

    test = await s.post(f"/api/v1/integrations/{iid}/test")
    assert test.status_code == 200, test.text
    assert test.json()["ok"] is True, test.json()
    assert test.json()["details"]["host_key_pinned_now"] is True
    keys = (await s.get(f"/api/v1/integrations/{iid}/host-keys")).json()
    assert len(keys) == 1 and keys[0]["port"] == port

    r = await s.post(f"/api/v1/integrations/{iid}/discover")
    assert r.status_code == 202, r.text
    await _drain(app)
    runs = (await s.get(f"/api/v1/integrations/{iid}/runs")).json()["items"]
    assert runs[0]["status"] == "completed", runs[0]
    assert runs[0]["stats"]["added"] == 3 and runs[0]["stats"]["relationships_added"] == 2

    res = await _resources(s)
    assert set(res) == {"app-01", "web", "migrate"}
    app01, web, migrate = res["app-01"], res["web"], res["migrate"]
    assert app01["resource_type"] == "linux_server" and app01["health_status"] == "degraded"
    assert web["health_status"] == "healthy" and migrate["health_status"] == "unhealthy"
    assert app01["integration_id"] == iid

    detail = (await s.get(f"/api/v1/resources/{app01['id']}")).json()
    assert detail["integration_name"] == "Linux hosts"
    assert detail["facts"]["docker"]["server_version"] == "27.3.1"
    assert "s3cret" not in str(detail)

    deps = (await s.get(f"/api/v1/resources/{app01['id']}/dependencies")).json()
    assert set(deps["blast_radius"]) == {web["id"], migrate["id"]}
    up = (await s.get(f"/api/v1/resources/{web['id']}/dependencies?direction=up")).json()
    assert {n["name"]: n["direction"] for n in up["nodes"]} == {"web": "self", "app-01": "upstream"}

    metrics = (await s.get(f"/api/v1/resources/{app01['id']}/metrics")).json()
    assert len(metrics) == 1 and metrics[0]["memory_percent"] == 75.0
    hist = (await s.get(f"/api/v1/resources/{app01['id']}/history")).json()
    assert [e["event_type"] for e in hist["items"]] == ["discovered"]

    summary = (await s.get("/api/v1/resources/summary")).json()
    assert summary["total"] == 3 and summary["by_health"]["degraded"] == 1
    assert summary["last_discovery_at"]

    # Second run: the migrate container is gone and a service was added.
    host.containers = [CONTAINERS[0]]
    host.linux_output = LINUX_OUTPUT.replace(
        "docker.service", "redis.service        loaded active running Redis\ndocker.service"
    )
    await s.post(f"/api/v1/integrations/{iid}/discover")
    await _drain(app)
    res = await _resources(s)
    assert res["migrate"]["stale"] is True and res["migrate"]["health_status"] == "unknown"
    hist = (await s.get(f"/api/v1/resources/{app01['id']}/history")).json()["items"]
    changed = next(e for e in hist if e["event_type"] == "changed")
    assert changed["details"]["changes"]["services"] == {"added": ["redis"], "removed": []}
    deps = (await s.get(f"/api/v1/resources/{app01['id']}/dependencies")).json()
    assert deps["blast_radius"] == [web["id"]]  # stale container's edge was removed

    # Single-resource health check re-collects just that target.
    chk = await s.post(f"/api/v1/resources/{app01['id']}/health/check")
    assert chk.status_code == 202, chk.text
    await _drain(app)
    run = (await s.get(f"/api/v1/integrations/runs/{chk.json()['id']}")).json()
    assert run["status"] == "completed" and run["stats"]["only_target"] == "127.0.0.1"
    manual = (
        await s.post("/api/v1/resources", json={"name": "printer", "resource_type": "other"})
    ).json()
    assert (await s.post(f"/api/v1/resources/{manual['id']}/health/check")).status_code == 409


async def test_changed_host_key_is_refused_and_audited(
    app: FastAPI, make_tenant: Any, ssh_host: tuple[FakeHost, int]
) -> None:
    host, port = ssh_host
    t: TenantInfo = await make_tenant()
    s = t.admin
    integration = await _linux_integration(s, port)
    other = asyncssh.generate_private_key("ssh-ed25519")
    async with tenant_session(uuid.UUID(t.id)) as session:
        session.add(
            SshHostKey(
                tenant_id=uuid.UUID(t.id),
                host="127.0.0.1",
                port=port,
                key_type="ssh-ed25519",
                public_key=other.export_public_key("openssh").decode().strip(),
                fingerprint=other.get_fingerprint("sha256"),
            )
        )
    await s.post(f"/api/v1/integrations/{integration['id']}/discover")
    await _drain(app)
    run = (await s.get(f"/api/v1/integrations/{integration['id']}/runs")).json()["items"][0]
    assert run["status"] == "failed"
    assert run["target_results"][0]["error_code"] == "host_key_changed"
    assert host.commands == []  # nothing executed on the impostor
    actions = await _audit_actions(t.id, integration["id"])
    assert "integration.host_key_mismatch" in actions

    # Admin verifies and resets the pin; the next run re-pins and succeeds.
    keys = (await s.get(f"/api/v1/integrations/{integration['id']}/host-keys")).json()
    r = await s.delete(f"/api/v1/integrations/{integration['id']}/host-keys/{keys[0]['id']}")
    assert r.status_code == 204
    await s.post(f"/api/v1/integrations/{integration['id']}/discover")
    await _drain(app)
    run = (await s.get(f"/api/v1/integrations/{integration['id']}/runs")).json()["items"][0]
    assert run["status"] == "completed", run


async def test_scheduler_queues_due_integrations(
    app: FastAPI, make_tenant: Any, ssh_host: tuple[FakeHost, int]
) -> None:
    _, port = ssh_host
    t: TenantInfo = await make_tenant()
    integration = await _linux_integration(t.admin, port, schedule_minutes=30)
    dispatcher: InlineDispatcher = app.state.dispatcher
    assert await schedule_due(dispatcher) >= 1
    await _drain(app)
    runs = (await t.admin.get(f"/api/v1/integrations/{integration['id']}/runs")).json()
    assert runs["items"][0]["trigger"] == "scheduled"
    assert runs["items"][0]["status"] == "completed"
    fresh = (await t.admin.get(f"/api/v1/integrations/{integration['id']}")).json()
    assert fresh["next_run_at"] > fresh["last_run_at"]
    # Not due again until next_run_at.
    before = runs["total"]
    await schedule_due(dispatcher)
    await _drain(app)
    after = (await t.admin.get(f"/api/v1/integrations/{integration['id']}/runs")).json()
    assert after["total"] == before


# ------------------------------------------------------------------ inventory


async def test_manual_inventory_relationships_and_isolation(make_tenant: Any) -> None:
    a: TenantInfo = await make_tenant()
    b: TenantInfo = await make_tenant()
    s = a.admin

    async def make(name: str, rtype: str, **kw: Any) -> str:
        r = await s.post("/api/v1/resources", json={"name": name, "resource_type": rtype, **kw})
        assert r.status_code == 201, r.text
        return str(r.json()["id"])

    app_id = await make("payroll", "application", environment="production", tags=["finance"])
    db_id = await make(
        "payroll-db", "database", environment="production", ip_addresses=["10.1.1.5"]
    )
    san_id = await make("san-01", "other", group_name="storage")

    async def link(src: str, dst: str, rtype: str) -> Any:
        return await s.post(
            "/api/v1/resource-relationships",
            json={"source_id": src, "target_id": dst, "relationship_type": rtype},
        )

    assert (await link(app_id, db_id, "DEPENDS_ON")).status_code == 201
    assert (await link(db_id, san_id, "STORES_DATA_IN")).status_code == 201
    assert (await link(app_id, db_id, "DEPENDS_ON")).status_code == 409
    assert (await link(app_id, app_id, "DEPENDS_ON")).status_code == 422

    deps = (await s.get(f"/api/v1/resources/{san_id}/dependencies")).json()
    assert deps["blast_radius"] == [db_id, app_id]  # ordered by distance
    up = (await s.get(f"/api/v1/resources/{app_id}/dependencies?direction=up")).json()
    assert {n["id"] for n in up["nodes"]} == {app_id, db_id, san_id}
    assert len(up["edges"]) == 2
    graph = (await s.get("/api/v1/resources/graph")).json()
    assert len(graph["nodes"]) == 3 and len(graph["edges"]) == 2
    env_graph = (
        await s.get("/api/v1/resources/graph", params={"environment": "production"})
    ).json()
    assert len(env_graph["nodes"]) == 2

    assert set(await _resources(s, q="payroll")) == {"payroll", "payroll-db"}
    assert set(await _resources(s, q="10.1.1")) == {"payroll-db"}
    assert set(await _resources(s, tag="finance")) == {"payroll"}
    assert set(await _resources(s, type="database")) == {"payroll-db"}
    facets = (await s.get("/api/v1/resources/facets")).json()
    assert facets == {"environments": ["production"], "groups": ["storage"], "tags": ["finance"]}

    r = await s.put(f"/api/v1/resources/{app_id}", json={"owner": "finance-team", "tags": []})
    assert r.status_code == 200 and r.json()["owner"] == "finance-team" and r.json()["tags"] == []
    hist = (await s.get(f"/api/v1/resources/{app_id}/history")).json()["items"]
    assert {"created", "relationship_added", "updated"} <= {e["event_type"] for e in hist}

    # Tenant B sees nothing and cannot reference A's resources.
    other = b.admin
    assert (await other.get(f"/api/v1/resources/{app_id}")).status_code == 404
    assert (await other.get("/api/v1/resources")).json()["total"] == 0
    assert (await other.get(f"/api/v1/resources/{app_id}/dependencies")).status_code == 404
    r = await other.post(
        "/api/v1/resource-relationships",
        json={"source_id": app_id, "target_id": db_id, "relationship_type": "DEPENDS_ON"},
    )
    assert r.status_code == 404
    assert (await other.get("/api/v1/resources/graph")).json()["nodes"] == []

    rels = (await s.get(f"/api/v1/resources/{db_id}/relationships")).json()
    assert len(rels) == 2
    assert (await s.delete(f"/api/v1/resources/{db_id}")).status_code == 204
    graph = (await s.get("/api/v1/resources/graph")).json()
    assert graph["edges"] == []  # cascaded


async def test_inventory_permissions(make_tenant: Any, login: LoginFn) -> None:
    t: TenantInfo = await make_tenant()
    r = await t.admin.post(
        "/api/v1/users",
        json={
            "username": "viewer",
            "email": f"viewer@{t.slug}.test",
            "password": STRONG,
            "role_ids": [await role_id(t.admin, "Read Only")],
        },
    )
    assert r.status_code == 201, r.text
    viewer = await login(t.slug, "viewer", STRONG)
    assert (await viewer.get("/api/v1/resources")).status_code == 200
    assert (await viewer.get("/api/v1/integrations")).status_code == 200
    forbidden = [
        await viewer.post("/api/v1/resources", json={"name": "x", "resource_type": "other"}),
        await viewer.post("/api/v1/integrations", json={"name": "x", "integration_type": "http"}),
    ]
    assert all(r.status_code == 403 for r in forbidden)


# ------------------------------------------------------------------ AI integration


async def test_investigation_uses_inventory_evidence(
    app: FastAPI, make_tenant: Any, fake_llm: FakeLLM
) -> None:
    t: TenantInfo = await make_tenant()
    s = t.admin
    r = await s.post(
        "/api/v1/models",
        json={
            "name": "Local",
            "provider": "ollama",
            "base_url": "http://fake-llm:11434",
            "model_name": "qwen2.5:7b",
        },
    )
    assert r.status_code == 201
    api = (
        await s.post(
            "/api/v1/resources",
            json={"name": "payroll-api", "resource_type": "application", "environment": "prod"},
        )
    ).json()
    db = (
        await s.post("/api/v1/resources", json={"name": "db-prod-01", "resource_type": "database"})
    ).json()
    await s.post(
        "/api/v1/resource-relationships",
        json={"source_id": api["id"], "target_id": db["id"], "relationship_type": "DEPENDS_ON"},
    )
    fake_llm.plan = {
        "tasks": [
            {"agent": "discovery", "objective": "Collect inventory evidence", "rationale": "x"},
            {"agent": "network_diagnostics", "objective": "Check DNS", "rationale": "y"},
        ]
    }
    r = await s.post("/api/v1/investigations", json={"request": "Payroll API is slow"})
    assert r.status_code == 202
    await _drain(app)
    inv = (await s.get(f"/api/v1/investigations/{r.json()['id']}")).json()
    assert inv["status"] == "completed", inv
    assert set(inv["resource_ids"]) == {api["id"], db["id"]}
    report = inv["result"]
    assert report["classification_verified"] is True
    assert {x["name"] for x in report["resources"] if x["resolved"]} == {
        "payroll-api",
        "db-prod-01",
    }
    tasks = {x["agent"]: x for x in report["tasks"]}
    assert tasks["discovery"]["status"] == "completed"
    assert tasks["network_diagnostics"]["status"] == "skipped"
    evidence = {e["resource"]: e for e in report["evidence"]}
    assert set(evidence) == {"payroll-api", "db-prod-01"}
    assert evidence["payroll-api"]["depends_on"] == ["db-prod-01"]
    assert evidence["db-prod-01"]["dependents"] == 1
    assert report["root_cause"] is None
    assert "Inventory evidence was collected for 2 resource(s)" in report["conclusion"]
    # Completed work is evidence, not a next step; skipped work remains.
    assert report["recommended_next_steps"] == ["Network Diagnostics: Check DNS"]
