"""Phase 2 end-to-end: models, agents, investigations, checkpoints, SSE, recovery."""

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.core.config import get_settings
from app.database.session import get_engine, tenant_session
from app.graphs.dispatch import InlineDispatcher
from app.graphs.runner import run_investigation
from app.models import Investigation
from tests.fakes import VAGUE, FakeLLM, SimulatedCrashError
from tests.integration.conftest import Session, TenantInfo

pytestmark = pytest.mark.usefixtures("bootstrapped")

MODEL = {
    "name": "Local Ollama",
    "provider": "ollama",
    "base_url": "http://fake-llm:11434",
    "model_name": "qwen2.5:7b",
}


async def _drain(app: FastAPI) -> None:
    dispatcher: InlineDispatcher = app.state.dispatcher
    await dispatcher.drain()


async def _tenant_with_model(make_tenant: Any, **model: Any) -> TenantInfo:
    t: TenantInfo = await make_tenant()
    r = await t.admin.post("/api/v1/models", json={**MODEL, **model})
    assert r.status_code == 201, r.text
    return t


async def _start(s: Session, request: str = "Payroll API is slow since 9am") -> str:
    r = await s.post("/api/v1/investigations", json={"request": request})
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued"
    return str(r.json()["id"])


# ------------------------------------------------------------------ models / agents


async def test_model_registry_and_agents(make_tenant: Any, fake_llm: FakeLLM) -> None:
    t: TenantInfo = await make_tenant()
    s = t.admin

    # Tenant gets the full agent catalogue on creation.
    agents = (await s.get("/api/v1/agents")).json()
    assert agents[0]["agent_type"] == "supervisor" and agents[0]["implemented"]
    assert len(agents) == 11
    assert {a["agent_type"]: a["available_in_phase"] for a in agents}["knowledge"] == 4

    r = await s.post("/api/v1/models", json={**MODEL, "api_key": "sk-secret"})
    assert r.status_code == 201, r.text
    model = r.json()
    assert model["is_default"] is True  # first model becomes the default
    assert model["has_api_key"] is True
    assert "sk-secret" not in r.text and "encrypted" not in r.text

    second = (
        await s.post("/api/v1/models", json={**MODEL, "name": "vLLM", "provider": "vllm"})
    ).json()
    assert second["is_default"] is False
    dup = await s.post("/api/v1/models", json=MODEL)
    assert dup.status_code == 409
    bad = await s.post("/api/v1/models", json={**MODEL, "name": "x", "base_url": "ftp://x"})
    assert bad.status_code == 422

    # Switching the default clears the old one.
    r = await s.put(f"/api/v1/models/{second['id']}", json={"is_default": True})
    assert r.json()["is_default"] is True
    models = {m["name"]: m for m in (await s.get("/api/v1/models")).json()}
    assert models["Local Ollama"]["is_default"] is False

    # Connectivity test uses the stored (decrypted) key and records health.
    r = await s.post(f"/api/v1/models/{model['id']}/test")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["model_available"] is True
    assert fake_llm.auth_headers[-1] == "Bearer sk-secret"
    m = (await s.get(f"/api/v1/models/{model['id']}")).json()
    assert m["last_status"] == "healthy" and m["last_latency_ms"] is not None

    # Pin an agent to a model; deleting the model unpins it.
    supervisor = agents[0]
    r = await s.put(
        f"/api/v1/agents/{supervisor['id']}",
        json={"model_id": model["id"], "configuration": {"temperature": 0.3}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["model_name"] == "Local Ollama"
    assert r.json()["configuration"]["temperature"] == 0.3
    assert r.json()["configuration"]["max_tokens"] == 1024
    assert (await s.delete(f"/api/v1/models/{model['id']}")).status_code == 204
    assert (await s.get(f"/api/v1/agents/{supervisor['id']}")).json()["model_id"] is None

    r = await s.put(f"/api/v1/agents/{supervisor['id']}", json={"model_id": str(uuid.uuid4())})
    assert r.status_code == 422

    audit = (await s.get("/api/v1/audit-logs", params={"entity_type": "model"})).json()
    actions = {a["action"] for a in audit["items"]}
    assert {"model.create", "model.update", "model.test", "model.delete"} <= actions


async def test_models_are_tenant_isolated(make_tenant: Any, fake_llm: FakeLLM) -> None:
    a = await _tenant_with_model(make_tenant)
    b: TenantInfo = await make_tenant()
    model_id = (await a.admin.get("/api/v1/models")).json()[0]["id"]
    assert (await b.admin.get("/api/v1/models")).json() == []
    assert (await b.admin.get(f"/api/v1/models/{model_id}")).status_code == 404
    b_agent = (await b.admin.get("/api/v1/agents")).json()[0]
    r = await b.admin.put(f"/api/v1/agents/{b_agent['id']}", json={"model_id": model_id})
    assert r.status_code == 422


# ------------------------------------------------------------------ investigations


async def test_investigation_end_to_end(app: FastAPI, make_tenant: Any, fake_llm: FakeLLM) -> None:
    t = await _tenant_with_model(make_tenant)
    inv_id = await _start(t.admin)
    await _drain(app)

    inv = (await t.admin.get(f"/api/v1/investigations/{inv_id}")).json()
    assert inv["status"] == "completed", inv
    assert inv["request_type"] == "incident_investigation"
    assert inv["username"] == "tadmin"
    report = inv["result"]
    assert report["evidence"] == [] and report["root_cause"] is None
    # planned diagnostics ran, found nothing resolvable, and did not guess
    assert [t["agent"] for t in report["tasks"]] == [
        "infrastructure_diagnostics",
        "application_diagnostics",
        "knowledge",
    ]
    assert {t["status"] for t in report["tasks"]} == {"skipped"}

    events = (await t.admin.get(f"/api/v1/investigations/{inv_id}/events")).json()
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(events) + 1))
    types = [e["event_type"] for e in events]
    assert types[0] == "investigation.queued"
    assert types[1] == "investigation.started"
    assert types[-1] == "investigation.completed"
    assert "rca.generated" in types and "tool.started" not in types

    execs = (
        await t.admin.get("/api/v1/agents/executions", params={"investigation_id": inv_id})
    ).json()
    # classify, resolve, plan, 3 specialists, aggregate, root cause, report
    assert execs["total"] == 9
    assert {e["status"] for e in execs["items"]} == {"success"}
    assert sum(e["llm_calls"] for e in execs["items"]) == 4  # + one tool turn per diagnostic

    agents = {a["agent_type"]: a for a in (await t.admin.get("/api/v1/agents")).json()}
    assert agents["supervisor"]["stats"]["executions_24h"] == 5

    listing = (await t.admin.get("/api/v1/investigations", params={"q": "payroll"})).json()
    assert listing["total"] == 1

    summary = (await t.admin.get("/api/v1/dashboard/summary")).json()
    assert summary["active_investigations"] == 0
    assert summary["investigations_24h"] == 1
    assert summary["agent_executions_24h"] == 9
    assert summary["default_model"] == "Local Ollama"


async def test_investigation_without_model_fails_clearly(
    app: FastAPI, make_tenant: Any, fake_llm: FakeLLM
) -> None:
    t: TenantInfo = await make_tenant()
    inv_id = await _start(t.admin)
    await _drain(app)
    inv = (await t.admin.get(f"/api/v1/investigations/{inv_id}")).json()
    assert inv["status"] == "failed"
    assert "No AI model is configured" in inv["error"]


async def test_autonomous_mode_rejected(make_tenant: Any) -> None:
    t: TenantInfo = await make_tenant()
    r = await t.admin.post(
        "/api/v1/investigations", json={"request": "restart nginx", "execution_mode": "autonomous"}
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "mode_unavailable"


async def test_clarification_survives_restart(
    app: FastAPI, make_tenant: Any, fake_llm: FakeLLM
) -> None:
    fake_llm.classifications = [VAGUE, {**VAGUE, "needs_clarification": False}]
    t = await _tenant_with_model(make_tenant)
    inv_id = await _start(t.admin, "it is broken again")
    await _drain(app)

    inv = (await t.admin.get(f"/api/v1/investigations/{inv_id}")).json()
    assert inv["status"] == "awaiting_input"
    assert inv["pending_question"] == "Which system is affected?"

    # Resuming a non-waiting investigation is rejected; a waiting one continues on a
    # *fresh* dispatcher/checkpointer (as after an API + worker restart).
    app.state.dispatcher = InlineDispatcher(get_settings(), app.state.redis)
    r = await t.admin.post(
        f"/api/v1/investigations/{inv_id}/resume", json={"answer": "The payroll API"}
    )
    assert r.status_code == 202, r.text
    await _drain(app)
    inv = (await t.admin.get(f"/api/v1/investigations/{inv_id}")).json()
    assert inv["status"] == "completed", inv
    assert inv["result"]["clarifications"] == [
        {"question": "Which system is affected?", "answer": "The payroll API"}
    ]
    again = await t.admin.post(f"/api/v1/investigations/{inv_id}/resume", json={"answer": "x"})
    assert again.status_code == 409


async def test_crash_recovery_resumes_from_checkpoint(
    app: FastAPI, make_tenant: Any, fake_llm: FakeLLM
) -> None:
    t = await _tenant_with_model(make_tenant)
    fake_llm.crash_on_plan = True
    inv_id = await _start(t.admin)
    await _drain(app)  # the run "dies" in plan_tasks
    inv = (await t.admin.get(f"/api/v1/investigations/{inv_id}")).json()
    assert inv["status"] == "running"

    # The worker's startup recovery finds it ...
    async with get_engine().connect() as conn:
        rows = (
            await conn.execute(text("SELECT id, status FROM worker_recoverable_investigations()"))
        ).all()
    assert (uuid.UUID(inv_id), "running") in [(r[0], r[1]) for r in rows]

    # ... and re-running continues after the last checkpoint: classify is not repeated.
    status = await run_investigation(
        get_settings(),
        tenant_id=uuid.UUID(t.id),
        investigation_id=uuid.UUID(inv_id),
        redis=app.state.redis,
    )
    assert status == "completed"
    assert fake_llm.calls == ["classify", "plan-crash", "plan", "tools", "tools"]
    events = (await t.admin.get(f"/api/v1/investigations/{inv_id}/events")).json()
    assert "investigation.resumed" in [e["event_type"] for e in events]
    # Running again is a no-op.
    assert (
        await run_investigation(
            get_settings(),
            tenant_id=uuid.UUID(t.id),
            investigation_id=uuid.UUID(inv_id),
            redis=app.state.redis,
        )
        == "completed"
    )


async def test_cancel(app: FastAPI, make_tenant: Any, fake_llm: FakeLLM) -> None:
    fake_llm.classifications = [VAGUE]
    t = await _tenant_with_model(make_tenant)
    inv_id = await _start(t.admin, "something is off")
    await _drain(app)
    r = await t.admin.post(f"/api/v1/investigations/{inv_id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert (await t.admin.post(f"/api/v1/investigations/{inv_id}/cancel")).status_code == 409

    # Cancel requested while queued: the runner stops before doing any work.
    class Hold(InlineDispatcher):
        async def enqueue(self, *a: Any, **k: Any) -> None:
            self.held = (a, k)

    hold = Hold(get_settings(), app.state.redis)
    app.state.dispatcher = hold
    inv2 = await _start(t.admin)
    r = await t.admin.post(f"/api/v1/investigations/{inv2}/cancel")
    assert r.json()["cancel_requested"] is True and r.json()["status"] == "queued"
    calls_before = len(fake_llm.calls)
    status = await run_investigation(
        get_settings(),
        tenant_id=uuid.UUID(t.id),
        investigation_id=uuid.UUID(inv2),
        redis=app.state.redis,
    )
    assert status == "cancelled" and len(fake_llm.calls) == calls_before


async def test_investigations_are_tenant_isolated(
    app: FastAPI, make_tenant: Any, fake_llm: FakeLLM
) -> None:
    a = await _tenant_with_model(make_tenant)
    b: TenantInfo = await make_tenant()
    inv_id = await _start(a.admin)
    await _drain(app)
    for path in ("", "/events", "/stream"):
        r = await b.admin.get(f"/api/v1/investigations/{inv_id}{path}")
        assert r.status_code == 404, path
    assert (await b.admin.post(f"/api/v1/investigations/{inv_id}/cancel")).status_code == 404
    assert (await b.admin.get("/api/v1/investigations")).json()["total"] == 0

    # Checkpoints are protected by RLS on the thread_id tenant prefix.
    settings = get_settings()
    async with await psycopg.AsyncConnection.connect(
        settings.app_psycopg_conninfo, autocommit=True
    ) as conn:
        await conn.execute("SELECT set_config('app.tenant_id', %s, false)", (b.id,))
        cur = await conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (f"{a.id}:{inv_id}",)
        )
        assert (await cur.fetchone()) == (0,)
        await conn.execute("SELECT set_config('app.tenant_id', %s, false)", (a.id,))
        cur = await conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (f"{a.id}:{inv_id}",)
        )
        row = await cur.fetchone()
        assert row is not None and row[0] > 0
        # Writing a checkpoint for another tenant's thread is rejected.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, "
                "metadata) VALUES (%s, '', 'x', '{}', '{}')",
                (f"{b.id}:{uuid.uuid4()}",),
            )


async def test_viewer_cannot_start_investigations(
    make_tenant: Any, login: Any, fake_llm: FakeLLM
) -> None:
    from tests.integration.conftest import STRONG, role_id

    t: TenantInfo = await make_tenant()
    rid = await role_id(t.admin, "Read Only")
    r = await t.admin.post(
        "/api/v1/users",
        json={
            "username": "viewer",
            "email": "viewer@x.test",
            "password": STRONG,
            "role_ids": [rid],
        },
    )
    assert r.status_code == 201, r.text
    viewer = await login(t.slug, "viewer", STRONG)
    assert (await viewer.get("/api/v1/investigations")).status_code == 200
    assert (await viewer.get("/api/v1/agents")).status_code == 200
    r = await viewer.post("/api/v1/investigations", json={"request": "check db"})
    assert r.status_code == 403
    assert (await viewer.post("/api/v1/models", json=MODEL)).status_code == 403


# ------------------------------------------------------------------ SSE


def _parse_sse(body: str) -> list[dict[str, Any]]:
    out = []
    for block in body.split("\n\n"):
        fields: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith(":") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            fields[key] = value.strip()
        if "event" in fields:
            out.append(fields)
    return out


async def test_sse_replay_with_last_event_id(
    app: FastAPI, make_tenant: Any, fake_llm: FakeLLM
) -> None:
    t = await _tenant_with_model(make_tenant)
    inv_id = await _start(t.admin)
    await _drain(app)
    total = len((await t.admin.get(f"/api/v1/investigations/{inv_id}/events")).json())

    r = await t.admin.get(f"/api/v1/investigations/{inv_id}/stream")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(r.text)
    assert events[-1]["event"] == "end"
    assert json.loads(events[-1]["data"]) == {"status": "completed"}
    assert [int(e["id"]) for e in events[:-1]] == list(range(1, total + 1))
    first = json.loads(events[0]["data"])
    assert first["event_type"] == "investigation.queued" and first["seq"] == 1

    r = await t.admin.client.get(
        f"/api/v1/investigations/{inv_id}/stream",
        headers={**t.admin.headers, "Last-Event-ID": str(total - 2)},
    )
    resumed = _parse_sse(r.text)
    assert [int(e["id"]) for e in resumed[:-1]] == [total - 1, total]


async def test_runner_marks_unknown_status_noop(app: FastAPI) -> None:
    status = await run_investigation(
        get_settings(), tenant_id=uuid.uuid4(), investigation_id=uuid.uuid4(), redis=None
    )
    assert status is None


async def test_recoverable_excludes_finished(
    app: FastAPI, make_tenant: Any, fake_llm: FakeLLM
) -> None:
    t = await _tenant_with_model(make_tenant)
    inv_id = await _start(t.admin)
    await _drain(app)
    async with get_engine().connect() as conn:
        ids = [
            r[0]
            for r in await conn.execute(text("SELECT id FROM worker_recoverable_investigations()"))
        ]
    assert uuid.UUID(inv_id) not in ids
    async with tenant_session(uuid.UUID(t.id)) as s:
        inv = await s.get(Investigation, uuid.UUID(inv_id))
        assert inv is not None and inv.attempts == 1


_ = SimulatedCrashError  # imported for documentation of the crash simulation


async def test_worker_startup_checks(app: FastAPI, make_tenant: Any, fake_llm: FakeLLM) -> None:
    from app.graphs.checkpointer import verify_checkpoint_schema
    from app.worker import recover

    await verify_checkpoint_schema(get_settings())

    class Pool:
        def __init__(self) -> None:
            self.jobs: list[tuple[Any, ...]] = []

        async def enqueue_job(self, *args: Any, **kwargs: Any) -> None:
            self.jobs.append(args)

    t = await _tenant_with_model(make_tenant)
    app.state.dispatcher = type("Noop", (), {"enqueue": lambda *a, **k: _noop()})()
    inv_id = await _start(t.admin)
    pool = Pool()
    assert await recover({"redis": pool}) >= 1
    assert ("run_investigation", t.id, inv_id, None) in pool.jobs


async def _noop() -> None:
    return None
