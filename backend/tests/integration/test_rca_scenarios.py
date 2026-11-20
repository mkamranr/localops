"""Phase 4 end to end: the spec's three investigation scenarios, incidents and tools.

Real discovery over an in-process SSH server pins host keys and builds the inventory;
diagnostic tools then run over that SSH connection (Windows via a fake WinRM
transport), driven by a scripted tool-calling model.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import asyncssh
import httpx
import pytest
from fastapi import FastAPI

from app.database.session import tenant_session
from app.graphs.dispatch import InlineDispatcher
from app.integrations.ssh import CommandResult
from app.models import Resource
from app.tools.transport import Connection, DefaultTransport, set_transport_factory
from tests.fakes import FakeLLM
from tests.integration.conftest import Session, TenantInfo, role_id
from tests.inventory_fixtures import CONTAINERS, FakeHost, container, port_of, start_ssh_server

pytestmark = pytest.mark.usefixtures("bootstrapped")

MODEL = {
    "name": "Local model",
    "provider": "vllm",
    "base_url": "http://fake-llm:8000",
    "model_name": "qwen3-14b",
}

SNAPSHOT = """## uptime
 10:00:00 up 1 day,  2 users,  load average: 3.98, 3.90, 3.50
## cpus
4
## loadavg
3.98 3.90 3.50 5/312 4242
## vmstat (1s x3)
procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
 5  0      0 500000  10000 900000    0    0     0     1  900  800 97  3  0  0  0
## memory (MB)
               total        used        free
Mem:            7812        5000         488
## disk
Filesystem      Size  Used Avail Use% Mounted on
/dev/sda1        40G   37G  3.0G  93% /
## pressure
cpu: some avg10=85.00 avg60=80.00 avg300=70.00 total=1
## oom (kernel, last 24h)
"""

PS = (
    "  PID  PPID USER %CPU %MEM RSS ELAPSED STAT COMMAND\n"
    " 4242 1 app 97.5 1.2 12000 05:02:03 R python worker.py --loop --password hunter2\n"
    "  880 1 root 1.0 0.5 4000 1-00:00:00 S /usr/sbin/nginx\n"
)
JOURNAL = (
    "2026-11-19T09:00:01 app-01 worker[4242]: queue empty, retrying immediately\n"
    "2026-11-19T09:00:01 app-01 worker[4242]: queue empty, retrying immediately\n"
)
INSPECT = [
    {
        "Name": "/migrate",
        "RestartCount": 14,
        "State": {"Status": "restarting", "ExitCode": 1, "OOMKilled": False, "Running": False},
        "Config": {"Image": "shop/migrate:2", "Env": ["DATABASE_PASSWORD=s3cr3t", "MODE=prod"]},
        "HostConfig": {"RestartPolicy": {"Name": "always"}},
    }
]
LOGS = (
    "2026-11-19T09:10:00Z Starting migrations\n"
    "2026-11-19T09:10:00Z Error: DATABASE_URL is not set\n"
)


@dataclass
class DiagHost(FakeHost):
    """A fake host that also answers the diagnostic and remediation commands."""

    restarted: int = 0
    killed: bool = False

    def reply(self, command: str) -> tuple[int, str, str]:
        if command.startswith("docker restart"):
            self.commands.append(command)
            self.restarted += 1
            return 0, "migrate\n", ""
        if command.startswith("cmd=$(ps -o args= -p 4242"):
            self.commands.append(command)
            if "worker.py" not in command:
                return 0, "LOCALOPS_MISMATCHpython worker.py --loop\n", ""
            self.killed = True
            return 0, "LOCALOPS_RC=0\nLOCALOPS_GONE\n", ""
        if command.startswith("echo '## uptime'"):
            self.commands.append(command)
            return 0, SNAPSHOT, ""
        if command.startswith("ps -eo"):
            self.commands.append(command)
            if self.killed:
                return 0, "\n".join(line for line in PS.splitlines() if "4242" not in line), ""
            return 0, PS, ""
        if command.startswith("if command -v journalctl"):
            self.commands.append(command)
            return 0, JOURNAL, ""
        if command.startswith("docker inspect --type container"):
            self.commands.append(command)
            if self.restarted:
                running = {"Status": "running", "ExitCode": 0, "OOMKilled": False, "Running": True}
                return 0, json.dumps([{**INSPECT[0], "State": running}]), ""
            return 0, json.dumps(INSPECT), ""
        if command.startswith("docker logs"):
            self.commands.append(command)
            return 0, LOGS, ""
        return super().reply(command)


@pytest.fixture
async def diag_host() -> AsyncIterator[tuple[DiagHost, int]]:
    host = DiagHost()
    server = await start_ssh_server(host, asyncssh.generate_private_key("ssh-ed25519"))
    yield host, port_of(server)
    server.close()


async def _drain(app: FastAPI) -> None:
    dispatcher: InlineDispatcher = app.state.dispatcher
    await dispatcher.drain()


async def _discovered_tenant(
    app: FastAPI, make_tenant: Any, port: int
) -> tuple[TenantInfo, dict[str, dict[str, Any]], str]:
    t: TenantInfo = await make_tenant()
    s = t.admin
    assert (await s.post("/api/v1/models", json=MODEL)).status_code == 201
    r = await s.post(
        "/api/v1/integrations",
        json={
            "name": "Linux hosts",
            "integration_type": "linux_ssh",
            "configuration": {"username": "ops", "port": port, "timeout_seconds": 10},
            "credentials": {"password": "s3cret"},
            "targets": [{"host": "127.0.0.1", "name": "app-01"}],
        },
    )
    assert r.status_code == 201, r.text
    iid = r.json()["id"]
    assert (await s.post(f"/api/v1/integrations/{iid}/discover")).status_code == 202
    await _drain(app)
    res = (await s.get("/api/v1/resources", params={"page_size": 100})).json()["items"]
    return t, {x["name"]: x for x in res}, iid


async def _investigation(s: Session, inv_id: str) -> dict[str, Any]:
    inv = (await s.get(f"/api/v1/investigations/{inv_id}")).json()
    assert inv["status"] == "completed", inv
    return dict(inv)


def _assert_cited(report: dict[str, Any]) -> None:
    """Every finding and the root cause cite evidence that exists."""
    ids = {e["id"] for e in report["evidence"]}
    assert all(e.get("id") for e in report["evidence"])
    for f in report["findings"]:
        if f["kind"] != "hypothesis":
            assert f["evidence_ids"] and set(f["evidence_ids"]) <= ids, f
    if report["root_cause"]:
        assert set(report["root_cause"]["evidence_ids"]) <= ids


# ---------------------------------------------------------------- scenario A


async def test_scenario_a_linux_cpu(
    app: FastAPI, make_tenant: Any, fake_llm: FakeLLM, diag_host: tuple[DiagHost, int]
) -> None:
    host, port = diag_host
    t, _res, _ = await _discovered_tenant(app, make_tenant, port)
    s = t.admin
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
        "tasks": [
            {"agent": "infrastructure_diagnostics", "objective": "Find what consumes CPU on app-01"}
        ]
    }
    fake_llm.tool_script = {
        "infrastructure_diagnostics": [
            [("linux_system_snapshot", {"resource_id": "app-01"})],
            [
                ("linux_top_processes", {"resource_id": "app-01", "sort_by": "cpu"}),
                ("linux_journal", {"resource_id": "app-01", "since_minutes": 60}),
                ("linux_service_status", {"resource_id": "app-01", "unit": "x; reboot"}),
            ],
        ]
    }
    fake_llm.rca_statement = "python worker.py (PID 4242) is in a busy loop and saturates the CPU"

    r = await s.post("/api/v1/investigations", json={"request": "Why is CPU usage high on app-01?"})
    assert r.status_code == 202, r.text
    await _drain(app)
    inv = await _investigation(s, r.json()["id"])
    report = inv["result"]

    tools = [e["tool"] for e in report["evidence"] if e["source"] == "tool"]
    assert tools[:3] == ["linux_system_snapshot", "linux_top_processes", "linux_journal"]
    assert report["root_cause"]["status"] == "confirmed"
    assert "worker.py" in report["root_cause"]["statement"]
    assert report["conclusion"].startswith("Root cause (confirmed")
    _assert_cited(report)
    assert any(c.startswith("ps -eo") for c in host.commands)
    # the injected unit name never reached the host
    assert not any("reboot" in c for c in host.commands)

    rows = (await s.get(f"/api/v1/investigations/{inv['id']}/tool-executions")).json()
    by_tool = {x["tool"]: x for x in rows}
    assert by_tool["linux_top_processes"]["status"] == "success"
    assert by_tool["linux_top_processes"]["evidence_id"] == "E1.2"
    assert (
        "hunter2" not in json.dumps(rows)
        and "[REDACTED]" in by_tool["linux_top_processes"]["output"]
    )
    assert by_tool["linux_service_status"]["status"] == "rejected"
    assert by_tool["linux_service_status"]["evidence_id"] is None

    events = (await s.get(f"/api/v1/investigations/{inv['id']}/events")).json()
    types = [e["event_type"] for e in events]
    assert types.count("tool.completed") >= 3 and "rca.generated" in types
    assert "finding.recorded" in types
    # history is persisted and searchable afterwards
    listing = (await s.get("/api/v1/investigations", params={"q": "CPU"})).json()
    assert listing["total"] == 1
    brief = listing["items"][0]["report"]
    assert brief["root_cause_status"] == "confirmed" and "worker.py" in brief["root_cause"]
    assert brief["confidence"] == report["confidence"] and brief["conclusion"]


# ---------------------------------------------------------------- scenario B


async def test_scenario_b_docker_restart_loop_from_incident(
    app: FastAPI, make_tenant: Any, fake_llm: FakeLLM, diag_host: tuple[DiagHost, int]
) -> None:
    host, port = diag_host
    t, res, iid = await _discovered_tenant(app, make_tenant, port)
    s = t.admin

    # discovery opened an incident for the unhealthy container, once
    incidents = (await s.get("/api/v1/incidents")).json()
    assert incidents["total"] == 1
    inc = incidents["items"][0]
    assert inc["source"] == "discovery" and inc["key"] == "INC-0001"
    assert inc["title"] == "migrate is unhealthy" and inc["status"] == "new"
    assert [r["name"] for r in inc["resources"]] == ["migrate"]
    assert (await s.post(f"/api/v1/integrations/{iid}/discover")).status_code == 202
    await _drain(app)
    assert (await s.get("/api/v1/incidents")).json()["total"] == 1  # de-duplicated

    fake_llm.classifications = [
        {
            "request_type": "incident_investigation",
            "summary": "The migrate container keeps restarting.",
            "affected_entities": ["migrate"],
            "symptoms": ["restart loop"],
            "urgency": "high",
            "confidence": 0.9,
        }
    ]
    fake_llm.plan = {"tasks": []}  # coverage adds the needed specialists
    fake_llm.tool_script = {
        "application_diagnostics": [
            [
                ("docker_container_state", {"resource_id": "migrate"}),
                ("docker_container_logs", {"resource_id": "migrate", "tail": 100}),
            ]
        ]
    }
    fake_llm.rca_statement = "The container exits because DATABASE_URL is not set"

    r = await s.post(f"/api/v1/incidents/{inc['id']}/investigate", json={})
    assert r.status_code == 202, r.text
    assert r.json()["incident_id"] == inc["id"]
    assert (await s.get(f"/api/v1/incidents/{inc['id']}")).json()["status"] == "investigating"
    await _drain(app)
    inv = await _investigation(s, r.json()["id"])
    report = inv["result"]
    agents = [x["agent"] for x in report["tasks"]]
    assert agents == ["incident_triage", "discovery", "application_diagnostics", "knowledge"]
    states = [e for e in report["evidence"] if e.get("tool") == "docker_container_state"]
    assert "14 restarts" in states[0]["summary"]
    assert "s3cr3t" not in json.dumps(report)
    assert report["root_cause"]["status"] == "confirmed"
    assert report["recommended_remediation"][0]["action"]
    assert report["triage"]["severity"] in ("medium", "high", "critical")
    _assert_cited(report)

    detail = (await s.get(f"/api/v1/incidents/{inc['id']}")).json()
    assert detail["root_cause"]["statement"] == fake_llm.rca_statement
    assert detail["investigation_summary"].startswith("Root cause (confirmed")
    assert [i["id"] for i in detail["investigations"]] == [inv["id"]]
    timeline = [
        e["event_type"] for e in (await s.get(f"/api/v1/incidents/{inc['id']}/timeline")).json()
    ]
    assert timeline[0] == "created"
    assert {"investigation_started", "investigation_completed"} <= set(timeline)
    diag = (await s.get(f"/api/v1/incidents/{inc['id']}/tool-executions")).json()
    assert {x["tool"] for x in diag} >= {"docker_container_state", "docker_container_logs"}

    # the container recovers: the incident timeline notes it
    host.containers = [CONTAINERS[0], container("migrate", state="running", image="shop/migrate:2")]
    assert (await s.post(f"/api/v1/integrations/{iid}/discover")).status_code == 202
    await _drain(app)
    timeline = (await s.get(f"/api/v1/incidents/{inc['id']}/timeline")).json()
    assert timeline[-1]["event_type"] == "resource_health"

    # close with a resolution; the similar-incident search finds it later
    closed = await s.post(
        f"/api/v1/incidents/{inc['id']}/close",
        json={"resolution": "Set DATABASE_URL in the compose file and redeployed"},
    )
    assert closed.status_code == 200 and closed.json()["status"] == "closed"
    other = await s.post(
        "/api/v1/incidents",
        json={
            "title": "migrate container restarting again",
            "resource_ids": [res["migrate"]["id"]],
        },
    )
    similar = (await s.get(f"/api/v1/incidents/{other.json()['id']}/similar")).json()
    assert similar[0]["number"] == "INC-0001"
    assert similar[0]["resolution"].startswith("Set DATABASE_URL")


# ---------------------------------------------------------------- scenario C


class FakeWinRM:
    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.restarted = False

    async def shell(self, command: str) -> CommandResult:
        raise AssertionError("not used")

    async def powershell(self, script: str) -> str:
        self.scripts.append(script)
        if "Restart-Service" in script:
            self.restarted = True
            return json.dumps(
                {
                    "found": True,
                    "name": "Spooler",
                    "display_name": "Print Spooler",
                    "status": "Running",
                }
            )
        if "Win32_Service" in script:
            return json.dumps(
                {
                    "name": "Spooler",
                    "display_name": "Print Spooler",
                    "state": "Running" if self.restarted else "Stopped",
                    "start_mode": "Auto",
                    "exit_code": 1067,
                    "depends_on": ["RPCSS=Running"],
                    "dependents": [],
                }
            )
        if "Get-WinEvent" in script:
            return json.dumps(
                [
                    {
                        "time": "2026-11-19T08:59:00",
                        "id": 7034,
                        "level": "Error",
                        "provider": "Service Control Manager",
                        "message": "The Print Spooler service terminated unexpectedly. It has done this 3 time(s).",
                    },
                    {
                        "time": "2026-11-19T08:58:00",
                        "id": 7031,
                        "level": "Error",
                        "provider": "Service Control Manager",
                        "message": "The Print Spooler service terminated unexpectedly.",
                    },
                ]
            )
        return json.dumps(
            {"cpu_percent": 5, "cpu_count": 8, "memory_total_mb": 16000, "memory_free_mb": 9000}
        )

    async def docker_get(self, path: str) -> httpx.Response:
        raise AssertionError("not used")


@pytest.fixture
def winrm() -> Iterator[FakeWinRM]:
    fake = FakeWinRM()

    def factory(conn: Connection, timeout: float) -> Any:
        return fake if conn.kind == "winrm" else DefaultTransport(conn, timeout)

    set_transport_factory(factory)
    yield fake
    set_transport_factory(None)


async def test_scenario_c_windows_service(
    app: FastAPI, make_tenant: Any, fake_llm: FakeLLM, winrm: FakeWinRM
) -> None:
    t: TenantInfo = await make_tenant()
    s = t.admin
    assert (await s.post("/api/v1/models", json=MODEL)).status_code == 201
    r = await s.post(
        "/api/v1/integrations",
        json={
            "name": "Windows",
            "integration_type": "windows_winrm",
            "configuration": {"username": "CORP\\svc-localops", "use_https": True},
            "credentials": {"password": "pw"},
            "targets": [{"host": "10.0.0.40", "name": "WIN-FS01"}],
        },
    )
    assert r.status_code == 201, r.text
    async with tenant_session(uuid.UUID(t.id)) as session:
        win = Resource(
            tenant_id=uuid.UUID(t.id),
            name="WIN-FS01",
            resource_type="windows_server",
            hostname="WIN-FS01.corp.local",
            ip_addresses=["10.0.0.40"],
            source="discovered",
            integration_id=uuid.UUID(r.json()["id"]),
            discovery_target="10.0.0.40",
            health_status="degraded",
            health_reasons=["Automatic service stopped: Spooler"],
            facts={},
        )
        session.add(win)
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
            [
                ("windows_service_status", {"resource_id": "WIN-FS01", "name": "Spooler"}),
                ("windows_event_log", {"resource_id": "WIN-FS01", "contains": "Print Spooler"}),
            ]
        ]
    }
    fake_llm.rca_statement = "The Print Spooler service crashed repeatedly (events 7031/7034)"

    r = await s.post(
        "/api/v1/investigations",
        json={"request": "Investigate why the Windows service stopped on WIN-FS01"},
    )
    await _drain(app)
    report = (await _investigation(s, r.json()["id"]))["result"]
    summaries = {e["tool"]: e["summary"] for e in report["evidence"] if e["source"] == "tool"}
    assert summaries["windows_service_status"].startswith("Print Spooler: Stopped")
    assert "7031" in summaries["windows_event_log"] and "7034" in summaries["windows_event_log"]
    assert report["root_cause"]["status"] == "confirmed"
    _assert_cited(report)
    assert any("('*' + 'Print Spooler' + '*')" in x for x in winrm.scripts)


# ---------------------------------------------------------------- incidents API


async def test_incident_lifecycle_and_permissions(
    app: FastAPI, make_tenant: Any, login: Any
) -> None:
    t: TenantInfo = await make_tenant()
    s = t.admin
    r = await s.post(
        "/api/v1/incidents",
        json={
            "title": "Payroll API slow",
            "description": "Users report timeouts",
            "severity": "high",
        },
    )
    assert r.status_code == 201, r.text
    inc = r.json()
    assert inc["key"] == "INC-0001" and inc["priority"] == "P2" and inc["status"] == "new"
    assert "awaiting_approval" not in inc["allowed_transitions"]

    bad = await s.put(f"/api/v1/incidents/{inc['id']}", json={"status": "remediating"})
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "invalid_transition"
    ok = await s.put(
        f"/api/v1/incidents/{inc['id']}", json={"status": "monitoring", "severity": "critical"}
    )
    assert ok.status_code == 200 and ok.json()["priority"] == "P1"
    c = await s.post(f"/api/v1/incidents/{inc['id']}/comments", json={"body": "Looking at the DB"})
    assert c.status_code == 201
    closed = await s.post(
        f"/api/v1/incidents/{inc['id']}/close", json={"resolution": "Index added"}
    )
    assert closed.json()["status"] == "closed" and closed.json()["closed_at"]
    again = await s.post(f"/api/v1/incidents/{inc['id']}/investigate", json={})
    assert again.status_code == 409  # reopen first
    reopened = await s.put(f"/api/v1/incidents/{inc['id']}", json={"status": "investigating"})
    assert reopened.json()["closed_at"] is None

    second = await s.post(
        "/api/v1/incidents", json={"title": "Disk full on db-01", "severity": "low"}
    )
    assert second.json()["key"] == "INC-0002"
    listing = (await s.get("/api/v1/incidents", params={"q": "INC-0002"})).json()
    assert [i["key"] for i in listing["items"]] == ["INC-0002"]
    stats = (await s.get("/api/v1/incidents/summary")).json()
    assert stats["open"] == 2 and stats["by_severity_open"] == {"critical": 1, "low": 1}
    dash = (await s.get("/api/v1/dashboard/summary")).json()
    assert dash["incidents"]["open_total"] == 2 and dash["incidents"]["critical"] == 1

    timeline = [
        e["event_type"] for e in (await s.get(f"/api/v1/incidents/{inc['id']}/timeline")).json()
    ]
    assert timeline[:2] == ["created", "updated"] and "comment" in timeline

    # read-only users can see but not change incidents, and cannot run tools
    users = await s.post(
        "/api/v1/users",
        json={
            "username": "viewer",
            "email": f"viewer@{t.slug}.test",
            "password": "Viewer-Pass-12345!",
            "role_ids": [await role_id(s, "Read Only")],
        },
    )
    assert users.status_code == 201, users.text
    viewer: Session = await login(t.slug, "viewer", "Viewer-Pass-12345!")
    assert (await viewer.get("/api/v1/incidents")).status_code == 200
    assert (await viewer.post("/api/v1/incidents", json={"title": "nope"})).status_code == 403
    run = await viewer.post(
        "/api/v1/tools/linux_journal/run", json={"resource_id": str(uuid.uuid4())}
    )
    assert run.status_code == 403
    tools = (await viewer.get("/api/v1/tools", params={"resource_type": "windows_server"})).json()
    assert "windows_event_log" in {x["name"] for x in tools}
    assert all(x["risk_level"] == "read" for x in tools if x["kind"] == "read")
    # change tools are listed (with their approval requirement) but never run directly
    assert {x["requires_approval"] for x in tools if x["kind"] == "change"} == {True}

    # another tenant cannot see these incidents
    other: TenantInfo = await make_tenant()
    assert (await other.admin.get(f"/api/v1/incidents/{inc['id']}")).status_code == 404
    assert (await other.admin.get("/api/v1/incidents")).json()["total"] == 0


async def test_tool_run_on_demand(
    app: FastAPI, make_tenant: Any, diag_host: tuple[DiagHost, int]
) -> None:
    _host, port = diag_host
    t, res, _ = await _discovered_tenant(app, make_tenant, port)
    s = t.admin
    r = await s.post(
        "/api/v1/tools/docker_container_logs/run",
        json={"resource_id": res["migrate"]["id"], "arguments": {"tail": 50}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "success" and "DATABASE_URL is not set" in body["output"]
    bad = await s.post(
        "/api/v1/tools/linux_journal/run",
        json={"resource_id": res["app-01"]["id"], "arguments": {"unit": "$(reboot)"}},
    )
    assert bad.json()["status"] == "rejected"
    history = (await s.get(f"/api/v1/resources/{res['migrate']['id']}/tool-executions")).json()
    assert history[0]["tool"] == "docker_container_logs" and history[0]["actor"] == "tadmin"


async def test_inventory_groups_containers_under_their_host(
    app: FastAPI, make_tenant: Any, diag_host: tuple[DiagHost, int]
) -> None:
    _host, port = diag_host
    t, res, _ = await _discovered_tenant(app, make_tenant, port)
    s = t.admin
    host, container = res["app-01"], res["migrate"]
    assert container["resource_type"] == "docker_container"
    # flat list: the container names its host; the host counts its containers by health
    assert container["parent_id"] == host["id"] and container["parent_name"] == "app-01"
    assert host["child_count"] >= 1 and host["child_health"].get("unhealthy", 0) >= 1

    top = (await s.get("/api/v1/resources", params={"top_level": True})).json()
    names = {r["name"] for r in top["items"]}
    assert "app-01" in names and "migrate" not in names
    assert top["total"] == len(top["items"])

    kids = (await s.get("/api/v1/resources", params={"parent_id": host["id"]})).json()
    assert {r["name"] for r in kids["items"]} >= {"migrate"}
    assert all(r["resource_type"] == "docker_container" for r in kids["items"])
    assert kids["total"] == host["child_count"]

    detail = (await s.get(f"/api/v1/resources/{container['id']}")).json()
    assert detail["parent_name"] == "app-01"
    detail = (await s.get(f"/api/v1/resources/{host['id']}")).json()
    assert detail["child_count"] == host["child_count"]
