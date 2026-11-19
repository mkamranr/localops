"""Phase 5 end to end: plan → policy → approval → execution gateway → verification.

The same in-process SSH host as the RCA scenarios answers the remediation commands
(``docker restart``, the guarded ``kill`` script); a scripted model proposes the plan.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text, update

from app.core.config import get_settings
from app.database.session import get_engine, tenant_session
from app.models import Approval, Resource
from app.remediation.gateway import execute_approved
from app.services import approval_service, policy_service
from tests.fakes import FakeLLM
from tests.integration.conftest import STRONG, Session, TenantInfo, role_id
from tests.integration.test_rca_scenarios import (
    MODEL,
    DiagHost,
    FakeWinRM,
    _discovered_tenant,
    _drain,
    diag_host,  # noqa: F401  (fixture)
    winrm,  # noqa: F401  (fixture)
)

pytestmark = pytest.mark.usefixtures("bootstrapped")

RESTART = {
    "tool": "docker_container_restart",
    "arguments": {"resource_id": "migrate", "timeout_seconds": 10},
    "rationale": "The container is in a restart loop (see the state evidence).",
    "expected_impact": "migrate is unavailable for a few seconds.",
    "evidence_ids": ["E1.1", "E2.1", "E3.1", "E9.9"],
}


def _docker_case(fake: FakeLLM, *steps: dict[str, Any], entities: list[str] | None = None) -> None:
    fake.classifications = [
        {
            "request_type": "incident_investigation",
            "summary": "The migrate container keeps restarting.",
            "affected_entities": entities or ["migrate"],
            "symptoms": ["restart loop"],
            "urgency": "high",
            "confidence": 0.9,
        }
    ]
    fake.plan = {
        "tasks": [{"agent": "application_diagnostics", "objective": "Why does migrate restart?"}]
    }
    fake.tool_script = {
        "application_diagnostics": [[("docker_container_state", {"resource_id": "migrate"})]]
    }
    fake.rca_statement = "The container exits because DATABASE_URL is not set"
    fake.remediation_plans = [{"steps": list(steps), "notes": "Set DATABASE_URL permanently."}]


async def _user(t: TenantInfo, login: Any, name: str, role: str) -> Session:
    r = await t.admin.post(
        "/api/v1/users",
        json={
            "username": name,
            "email": f"{name}@{t.slug}.test",
            "password": STRONG,
            "role_ids": [await role_id(t.admin, role)],
        },
    )
    assert r.status_code == 201, r.text
    session: Session = await login(t.slug, name, STRONG)
    return session


async def _start(s: Session, request: str, mode: str = "assisted") -> str:
    r = await s.post("/api/v1/investigations", json={"request": request, "execution_mode": mode})
    assert r.status_code == 202, r.text
    return str(r.json()["id"])


async def _inv(s: Session, inv_id: str) -> dict[str, Any]:
    return dict((await s.get(f"/api/v1/investigations/{inv_id}")).json())


async def _pending(s: Session, inv_id: str) -> dict[str, Any]:
    rows = (
        await s.get("/api/v1/approvals", params={"investigation_id": inv_id, "status": "pending"})
    ).json()
    assert rows["total"] == 1, rows
    return dict(rows["items"][0])


async def _events(s: Session, inv_id: str) -> list[str]:
    return [
        e["event_type"] for e in (await s.get(f"/api/v1/investigations/{inv_id}/events")).json()
    ]


# ------------------------------------------------------------------ scenario B


async def test_docker_restart_needs_a_second_person(
    app: FastAPI,
    make_tenant: Any,
    login: Any,
    fake_llm: FakeLLM,
    diag_host: tuple[DiagHost, int],  # noqa: F811
) -> None:
    host, port = diag_host
    t, _res, _ = await _discovered_tenant(app, make_tenant, port)
    s = t.admin
    inc = (await s.get("/api/v1/incidents")).json()["items"][0]
    _docker_case(fake_llm, RESTART)

    r = await s.post(f"/api/v1/incidents/{inc['id']}/investigate", json={})
    inv_id = r.json()["id"]
    await _drain(app)
    inv = await _inv(s, inv_id)
    assert inv["status"] == "awaiting_approval", inv
    assert inv["pending_question"].startswith("Restart the Docker container 'migrate'")
    assert (await s.get(f"/api/v1/incidents/{inc['id']}")).json()["status"] == "awaiting_approval"
    assert host.restarted == 0
    # the planner only saw evidence ids it can cite; the unknown one was dropped
    pending = await _pending(s, inv_id)
    assert pending["action"] == "docker_container_restart"
    assert pending["risk_level"] == "medium" and pending["auto_approved"] is False
    assert "E9.9" not in pending["evidence_ids"] and pending["evidence_ids"]
    assert pending["requested_by_username"] == "tadmin"

    detail = (await s.get(f"/api/v1/approvals/{pending['id']}")).json()
    assert detail["can_decide"] is False and "requested" in detail["cannot_decide_reason"]
    assert detail["incident_key"] == inc["key"] and detail["evidence"]
    summary = (await s.get("/api/v1/approvals/summary")).json()
    assert summary["pending"] == 1 and summary["by_risk"] == {"medium": 1}
    assert (await s.get("/api/v1/dashboard/summary")).json()["pending_approvals"] == 1

    # four eyes: the requester may not approve their own request
    own = await s.post(f"/api/v1/approvals/{pending['id']}/approve", json={})
    assert own.status_code == 403 and own.json()["error"]["code"] == "self_approval"
    # an operator may run remediation but not approve it
    operator = await _user(t, login, "operator", "Operator")
    assert (await operator.get(f"/api/v1/approvals/{pending['id']}")).status_code == 200
    assert (await operator.post(f"/api/v1/approvals/{pending['id']}/approve")).status_code == 403
    # another tenant sees nothing
    other: TenantInfo = await make_tenant()
    assert (await other.admin.get(f"/api/v1/approvals/{pending['id']}")).status_code == 404
    assert (await other.admin.get("/api/v1/approvals")).json()["total"] == 0

    approver = await _user(t, login, "approver", "Infrastructure Administrator")
    ok = await approver.post(
        f"/api/v1/approvals/{pending['id']}/approve", json={"comment": "go ahead"}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "approved" and ok.json()["approved_by_username"] == "approver"
    again = await approver.post(f"/api/v1/approvals/{pending['id']}/approve")
    assert again.status_code == 409
    await _drain(app)

    inv = await _inv(s, inv_id)
    assert inv["status"] == "completed", inv
    assert host.restarted == 1
    assert [c for c in host.commands if c.startswith("docker restart")] == [
        "docker restart -t 10 -- migrate"
    ]
    rem = inv["result"]["remediation"]
    assert rem["status"] == "verified"
    step = rem["steps"][0]
    assert step["status"] == "verified" and step["verification"]["status"] == "passed"
    assert "Remediation: 1 step(s) executed and verified" in inv["result"]["conclusion"]

    execs = (await s.get("/api/v1/executions", params={"investigation_id": inv_id})).json()
    assert execs["total"] == 1
    job = execs["items"][0]
    assert job["status"] == "succeeded" and job["verification_status"] == "passed"
    assert job["before"]["docker_container_state"]["data"]["state"]["status"] == "restarting"
    assert job["after"]["docker_container_state"]["data"]["state"]["status"] == "running"
    approval = (await s.get(f"/api/v1/approvals/{pending['id']}")).json()
    assert approval["status"] == "executed" and approval["execution"]["id"] == job["id"]

    incident = (await s.get(f"/api/v1/incidents/{inc['id']}")).json()
    assert incident["status"] == "monitoring"
    by_incident = {"incident_id": inc["id"]}
    assert (await s.get("/api/v1/executions", params=by_incident)).json()["total"] == 1
    assert (await s.get("/api/v1/approvals", params=by_incident)).json()["total"] == 1
    timeline = [
        e["event_type"] for e in (await s.get(f"/api/v1/incidents/{inc['id']}/timeline")).json()
    ]
    assert {"remediation_planned", "approval_requested", "remediation_started"} <= set(timeline)
    assert "remediation_verified" in timeline

    types = await _events(s, inv_id)
    for expected in (
        "remediation.planned",
        "approval.requested",
        "approval.awaiting",
        "approval.decided",
        "execution.started",
        "execution.completed",
        "verification.completed",
        "investigation.completed",
    ):
        assert expected in types, expected

    actions = {
        a["action"]
        for a in (await s.get("/api/v1/audit-logs", params={"page_size": 200})).json()["items"]
    }
    assert {"approval.request", "approval.approve", "remediation.execute"} <= actions

    # one job per approval, even when asked to run again concurrently
    results = await asyncio.gather(
        *(
            execute_approved(
                get_settings(),
                uuid.UUID(t.id),
                uuid.UUID(pending["id"]),
                rules_loader=policy_service.load_rules,
                before={},
            )
            for _ in range(3)
        )
    )
    assert all(r.duplicate for r in results) and {r.job_id for r in results} == {job["id"]}
    assert host.restarted == 1


async def test_request_changes_then_reject(
    app: FastAPI,
    make_tenant: Any,
    login: Any,
    fake_llm: FakeLLM,
    diag_host: tuple[DiagHost, int],  # noqa: F811
) -> None:
    host, port = diag_host
    t, _res, _ = await _discovered_tenant(app, make_tenant, port)
    _docker_case(fake_llm, RESTART)
    revised = {**RESTART, "arguments": {"resource_id": "migrate", "timeout_seconds": 30}}
    fake_llm.remediation_plans.append({"steps": [revised]})
    inv_id = await _start(t.admin, "migrate keeps restarting, please fix")
    await _drain(app)
    first = await _pending(t.admin, inv_id)
    approver = await _user(t, login, "approver", "Infrastructure Administrator")

    short = await approver.post(f"/api/v1/approvals/{first['id']}/request-changes", json={})
    assert short.status_code == 422
    r = await approver.post(
        f"/api/v1/approvals/{first['id']}/request-changes",
        json={"comment": "Give it 30 seconds to stop cleanly"},
    )
    assert r.status_code == 200 and r.json()["status"] == "changes_requested"
    await _drain(app)
    assert "<comment>Give it 30 seconds to stop cleanly</comment>" in fake_llm.planner_prompts[-1]
    second = await _pending(t.admin, inv_id)
    assert second["id"] != first["id"] and second["step_index"] == first["step_index"]
    assert second["action_parameters"]["timeout_seconds"] == 30

    r = await approver.post(
        f"/api/v1/approvals/{second['id']}/reject", json={"comment": "Fix the config instead"}
    )
    assert r.status_code == 200
    await _drain(app)
    inv = await _inv(t.admin, inv_id)
    assert inv["status"] == "completed"
    rem = inv["result"]["remediation"]
    assert rem["status"] == "not_executed"
    assert rem["steps"][0]["status"] == "rejected" and rem["steps"][0]["revisions"] == 1
    assert rem["steps"][0]["status_reason"] == "Fix the config instead"
    assert host.restarted == 0
    assert (await t.admin.get("/api/v1/executions")).json()["total"] == 0


# ------------------------------------------------------------------ scenario A


async def test_process_termination_refuses_the_wrong_process(
    app: FastAPI,
    make_tenant: Any,
    login: Any,
    fake_llm: FakeLLM,
    diag_host: tuple[DiagHost, int],  # noqa: F811
) -> None:
    host, port = diag_host
    t, _res, _ = await _discovered_tenant(app, make_tenant, port)
    approver = await _user(t, login, "approver", "Infrastructure Administrator")
    fake_llm.classifications = [
        {
            "request_type": "incident_investigation",
            "summary": "CPU usage is high on app-01.",
            "affected_entities": ["app-01"],
            "symptoms": ["high CPU"],
            "urgency": "high",
            "confidence": 0.9,
        }
    ]
    fake_llm.plan = {
        "tasks": [{"agent": "infrastructure_diagnostics", "objective": "What uses the CPU?"}]
    }
    fake_llm.tool_script = {
        "infrastructure_diagnostics": [
            [("linux_top_processes", {"resource_id": "app-01", "sort_by": "cpu"})]
        ]
    }
    fake_llm.rca_statement = "python worker.py (PID 4242) is in a busy loop"

    def kill(expected: str) -> dict[str, Any]:
        return {
            "tool": "linux_process_terminate",
            "arguments": {"resource_id": "app-01", "pid": 4242, "expected_command": expected},
            "rationale": "PID 4242 uses 97% CPU",
            "evidence_ids": ["E1.1"],
        }

    async def run(expected: str) -> dict[str, Any]:
        fake_llm.remediation_plans = [{"steps": [kill(expected)]}]
        inv_id = await _start(t.admin, "Why is CPU usage high on app-01?")
        await _drain(app)
        pending = await _pending(t.admin, inv_id)
        assert pending["risk_level"] == "high"
        assert "only if its command line contains" in pending["description"]
        assert (
            await approver.post(f"/api/v1/approvals/{pending['id']}/approve")
        ).status_code == 200
        await _drain(app)
        return await _inv(t.admin, inv_id)

    # a PID whose command line does not match is never signalled
    inv = await run("nginx-master")
    rem = inv["result"]["remediation"]
    assert rem["status"] == "stopped" and rem["steps"][0]["status"] == "failed"
    assert "does not contain 'nginx-master'" in rem["steps"][0]["status_reason"]
    assert not host.killed
    job = (await t.admin.get("/api/v1/executions")).json()["items"][0]
    assert job["status"] == "failed" and job["verification_status"] in ("pending", "skipped")
    assert "Remediation needs attention" in inv["result"]["recommended_next_steps"][0]

    inv = await run("worker.py")
    assert host.killed
    step = inv["result"]["remediation"]["steps"][0]
    assert step["status"] == "verified", step
    assert "no longer among the top processes" in step["verification"]["summary"]


# ------------------------------------------------------------------ modes


async def _set_environment(tenant_id: str, name: str, env: str) -> None:
    async with tenant_session(uuid.UUID(tenant_id)) as s:
        await s.execute(update(Resource).where(Resource.name == name).values(environment=env))


async def test_autonomous_runs_low_risk_but_pauses_on_critical(
    app: FastAPI,
    make_tenant: Any,
    login: Any,
    fake_llm: FakeLLM,
    diag_host: tuple[DiagHost, int],  # noqa: F811
) -> None:
    host, port = diag_host
    t, _res, _ = await _discovered_tenant(app, make_tenant, port)
    await _set_environment(t.id, "migrate", "dev")
    await _set_environment(t.id, "app-01", "dev")
    reboot = {
        "tool": "linux_reboot",
        "arguments": {"resource_id": "app-01", "delay_seconds": 5},
        "rationale": "Clear the host state",
        "evidence_ids": ["E1.1"],
    }
    _docker_case(fake_llm, RESTART, reboot, entities=["migrate", "app-01"])
    inv_id = await _start(t.admin, "migrate keeps restarting", mode="autonomous")
    await _drain(app)

    # the low-risk restart ran without a human ...
    assert host.restarted == 1
    approvals = await t.admin.get("/api/v1/approvals", params={"investigation_id": inv_id})
    by_action = {a["action"]: a for a in approvals.json()["items"]}
    auto = by_action["docker_container_restart"]
    assert auto["auto_approved"] is True and auto["status"] == "executed"
    assert auto["risk_level"] == "low"
    # ... the critical reboot waits for a second person, even in autonomous mode
    inv = await _inv(t.admin, inv_id)
    assert inv["status"] == "awaiting_approval"
    crit = by_action["linux_reboot"]
    assert crit["status"] == "pending" and crit["risk_level"] == "critical"
    assert crit["auto_approved"] is False
    assert "approval.auto_approved" in await _events(t.admin, inv_id)

    # cancelling the investigation withdraws the open approval
    r = await t.admin.post(f"/api/v1/investigations/{inv_id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    crit = (await t.admin.get(f"/api/v1/approvals/{crit['id']}")).json()
    assert crit["status"] == "cancelled"
    assert not any("reboot" in c for c in host.commands)


async def test_manual_mode_waits_for_a_request(
    app: FastAPI,
    make_tenant: Any,
    login: Any,
    fake_llm: FakeLLM,
    diag_host: tuple[DiagHost, int],  # noqa: F811
) -> None:
    host, port = diag_host
    t, _res, _ = await _discovered_tenant(app, make_tenant, port)
    _docker_case(fake_llm, RESTART)
    inv_id = await _start(t.admin, "migrate keeps restarting", mode="manual")
    await _drain(app)
    inv = await _inv(t.admin, inv_id)
    assert inv["status"] == "awaiting_approval"
    assert "remediation.awaiting_request" in await _events(t.admin, inv_id)
    assert (await t.admin.get("/api/v1/approvals")).json()["total"] == 0

    investigator = await _user(t, login, "inv", "Investigator")
    url = f"/api/v1/investigations/{inv_id}/remediation"
    denied = await investigator.post(f"{url}/0", json={"action": "request"})
    assert denied.status_code == 403
    assert (await t.admin.post(f"{url}/3", json={"action": "request"})).status_code == 409
    assert (await t.admin.post(f"{url}/0", json={"action": "run"})).status_code == 422
    r = await t.admin.post(f"{url}/0", json={"action": "request"})
    assert r.status_code == 202, r.text
    await _drain(app)
    pending = await _pending(t.admin, inv_id)
    approver = await _user(t, login, "approver", "Infrastructure Administrator")
    assert (await approver.post(f"/api/v1/approvals/{pending['id']}/approve")).status_code == 200
    await _drain(app)
    inv = await _inv(t.admin, inv_id)
    assert inv["status"] == "completed" and host.restarted == 1


async def test_requester_without_remediation_permission_gets_advice_only(
    app: FastAPI,
    make_tenant: Any,
    login: Any,
    fake_llm: FakeLLM,
    diag_host: tuple[DiagHost, int],  # noqa: F811
) -> None:
    host, port = diag_host
    t, _res, _ = await _discovered_tenant(app, make_tenant, port)
    _docker_case(fake_llm, RESTART)
    investigator = await _user(t, login, "inv", "Investigator")
    inv_id = await _start(investigator, "migrate keeps restarting")
    await _drain(app)
    inv = await _inv(investigator, inv_id)
    assert inv["status"] == "completed"
    rem = inv["result"]["remediation"]
    assert rem["status"] == "advisory" and rem["steps"][0]["status"] == "advisory"
    assert (await t.admin.get("/api/v1/approvals")).json()["total"] == 0
    assert host.restarted == 0


# ------------------------------------------------------------------ safety nets


async def test_expiry_and_kill_switch(
    app: FastAPI,
    make_tenant: Any,
    login: Any,
    fake_llm: FakeLLM,
    diag_host: tuple[DiagHost, int],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, port = diag_host
    t, _res, _ = await _discovered_tenant(app, make_tenant, port)
    _docker_case(fake_llm, RESTART)
    approver = await _user(t, login, "approver", "Infrastructure Administrator")

    # an approval nobody decides expires; the step is recorded as not executed
    inv_id = await _start(t.admin, "migrate keeps restarting")
    await _drain(app)
    pending = await _pending(t.admin, inv_id)
    async with tenant_session(uuid.UUID(t.id)) as s:
        await s.execute(
            update(Approval)
            .where(Approval.id == uuid.UUID(pending["id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
    async with get_engine().connect() as conn:
        rows = [
            (r[0], r[1], r[2])
            for r in (
                await conn.execute(
                    text("SELECT id, tenant_id, investigation_id FROM worker_expired_approvals()")
                )
            ).all()
        ]
    assert (uuid.UUID(pending["id"]), uuid.UUID(t.id), uuid.UUID(inv_id)) in rows
    done = await approval_service.expire_overdue(rows, app.state.dispatcher, None)
    assert done >= 1
    await _drain(app)
    inv = await _inv(t.admin, inv_id)
    assert inv["status"] == "completed"
    assert inv["result"]["remediation"]["steps"][0]["status"] == "expired"
    late = await approver.post(f"/api/v1/approvals/{pending['id']}/approve")
    assert late.status_code == 409

    # the kill switch stops an approved action at execution time
    inv_id = await _start(t.admin, "migrate keeps restarting")
    await _drain(app)
    pending = await _pending(t.admin, inv_id)
    monkeypatch.setattr(get_settings(), "remediation_enabled", False)
    assert (await approver.post(f"/api/v1/approvals/{pending['id']}/approve")).status_code == 200
    await _drain(app)
    inv = await _inv(t.admin, inv_id)
    step = inv["result"]["remediation"]["steps"][0]
    assert step["status"] == "failed" and "disabled" in step["status_reason"].lower()
    assert host.restarted == 0
    async with tenant_session(uuid.UUID(t.id)) as s:
        a = await s.scalar(select(Approval).where(Approval.id == uuid.UUID(pending["id"])))
        assert a is not None and a.status == "failed"


# ------------------------------------------------------------------ scenario C


async def test_windows_service_restart(
    app: FastAPI,
    make_tenant: Any,
    login: Any,
    fake_llm: FakeLLM,
    winrm: FakeWinRM,  # noqa: F811
) -> None:
    t: TenantInfo = await make_tenant()
    s = t.admin
    assert (await s.post("/api/v1/models", json=MODEL)).status_code == 201
    r = await s.post(
        "/api/v1/integrations",
        json={
            "name": "Windows",
            "integration_type": "windows_winrm",
            "configuration": {"username": r"CORP\svc-localops", "use_https": True},
            "credentials": {"password": "pw"},
            "targets": [{"host": "10.0.0.40", "name": "WIN-FS01"}],
        },
    )
    async with tenant_session(uuid.UUID(t.id)) as session:
        session.add(
            Resource(
                tenant_id=uuid.UUID(t.id),
                name="WIN-FS01",
                resource_type="windows_server",
                hostname="WIN-FS01.corp.local",
                ip_addresses=["10.0.0.40"],
                source="discovered",
                integration_id=uuid.UUID(r.json()["id"]),
                discovery_target="10.0.0.40",
                health_status="degraded",
                environment="production",
                facts={},
            )
        )
    fake_llm.classifications = [
        {
            "request_type": "incident_investigation",
            "summary": "The Print Spooler service stopped on WIN-FS01.",
            "affected_entities": ["WIN-FS01"],
            "symptoms": ["service stopped"],
            "urgency": "medium",
            "confidence": 0.9,
        }
    ]
    fake_llm.plan = {
        "tasks": [{"agent": "infrastructure_diagnostics", "objective": "Why did Spooler stop?"}]
    }
    fake_llm.tool_script = {
        "infrastructure_diagnostics": [
            [("windows_service_status", {"resource_id": "WIN-FS01", "name": "Spooler"})]
        ]
    }
    fake_llm.rca_statement = "The Print Spooler service crashed"
    fake_llm.remediation_plans = [
        {
            "steps": [
                {
                    "tool": "windows_service_restart",
                    "arguments": {"resource_id": "WIN-FS01", "name": "Spooler"},
                    "rationale": "Spooler is stopped",
                    "evidence_ids": ["E1.1"],
                }
            ]
        }
    ]
    inv_id = await _start(s, "Spooler stopped on WIN-FS01")
    await _drain(app)
    pending = await _pending(s, inv_id)
    assert pending["risk_level"] == "high"
    assert "Restart-Service -Name 'Spooler' -Force" in pending["description"]
    assert pending["policy"]["matched_rules"] == []  # restart is high by default
    assert not winrm.restarted
    approver = await _user(t, login, "approver", "Infrastructure Administrator")
    assert (await approver.post(f"/api/v1/approvals/{pending['id']}/approve")).status_code == 200
    await _drain(app)
    inv = await _inv(s, inv_id)
    assert winrm.restarted
    assert sum("Restart-Service" in x for x in winrm.scripts) == 1
    step = inv["result"]["remediation"]["steps"][0]
    assert step["status"] == "verified" and "Spooler is Running" in step["verification"]["summary"]
