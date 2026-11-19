"""Investigation graph behaviour with an in-memory runtime and checkpointer."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.assessments import Assessment
from app.agents.catalogue import get_spec
from app.agents.supervisor import PlannedTask, TaskPlan, ensure_coverage, sanitise_plan
from app.agents.toolloop import ToolBudget
from app.core.config import get_settings
from app.graphs.investigation import build_graph
from app.graphs.runtime import (
    AgentBinding,
    InvestigationCancelledError,
    InvestigationRuntime,
    NodeCall,
)
from app.llm.gateway import EndpointConfig, ModelClient
from app.tools.base import ToolSpec
from app.tools.executor import ToolRunResult
from tests.fakes import CLASSIFICATION, VAGUE, FakeLLM


class MemoryRuntime(InvestigationRuntime):
    """Runtime without a database: records events/executions in lists."""

    def __init__(self, fake: FakeLLM) -> None:
        super().__init__(
            settings=get_settings(),
            tenant_id=uuid.uuid4(),
            investigation_id=uuid.uuid4(),
            client_factory=lambda ep: ModelClient(ep, transport=fake.transport()),
        )
        self.events: list[tuple[str, str | None, dict[str, Any]]] = []
        self.executions: list[dict[str, Any]] = []
        self.cancelled = False
        # entity -> resolved resource summary
        self.inventory: dict[str, dict[str, Any]] = {}
        self.resource_ids: list[str] = []
        self.tool_runs: list[tuple[str, str, dict[str, Any], str]] = []
        self.tool_outputs: dict[str, tuple[str, str]] = {}

    async def emit(
        self, event_type: str, agent: str | None = None, data: dict[str, Any] | None = None
    ) -> int:
        self.events.append((event_type, agent, data or {}))
        return len(self.events)

    async def check_cancelled(self) -> None:
        if self.cancelled:
            raise InvestigationCancelledError

    async def bind_agent(self, agent_type: str) -> AgentBinding:
        spec = get_spec(agent_type)
        assert spec is not None
        return AgentBinding(
            spec,
            True,
            {"temperature": 0.1, "max_tokens": 512},
            EndpointConfig(provider="ollama", base_url="http://llm:11434", model="m"),
            None,
        )

    async def resolve_entities(self, entities: list[str]) -> list[dict[str, Any]]:
        out = []
        for e in entities:
            if e in self.inventory:
                out.append({"entity": e, "resolved": True, "match": "exact", **self.inventory[e]})
            else:
                out.append({"entity": e, "name": e, "resolved": False, "resource_id": None})
        return out

    async def inventory_evidence(self, resource_ids: list[uuid.UUID]) -> list[dict[str, Any]]:
        by_id = {r["resource_id"]: r for r in self.inventory.values()}
        return [
            {
                "source": "inventory",
                "agent": "discovery",
                "resource": by_id[str(i)]["name"],
                "health": by_id[str(i)]["health"],
                "summary": f"Health {by_id[str(i)]['health']}",
            }
            for i in resource_ids
            if str(i) in by_id
        ]

    async def set_resource_ids(self, resource_ids: list[str]) -> None:
        self.resource_ids = resource_ids

    async def resources_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        by_id = {r["resource_id"]: r for r in self.inventory.values()}
        return [
            {"entity": by_id[i]["name"], "resolved": True, **by_id[i]} for i in ids if i in by_id
        ]

    async def tool_budget(self) -> ToolBudget:
        if self._budget is None:
            self._budget = ToolBudget(self.settings.investigation_max_tool_calls)
        return self._budget

    async def run_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        evidence_id: str,
        *,
        agent_type: str,
        allowed: list[ToolSpec],
        allow_remote: bool,
    ) -> ToolRunResult:
        self.tool_runs.append((agent_type, name, arguments, evidence_id))
        if name not in {t.name for t in allowed}:
            return ToolRunResult(name, "rejected", "Rejected", f"ERROR: Unknown tool '{name}'")
        output = self.tool_outputs.get(name, (f"{name} ok", f"output of {name}"))
        return ToolRunResult(
            name,
            "success",
            output[0],
            output[1],
            arguments=arguments,
            resource_id=arguments.get("resource_id"),
            resource_name="app-01",
            resource_type="linux_server",
        )

    async def in_session(self, fn: Any) -> Any:
        return Assessment(note="No history in the in-memory runtime")

    async def start_execution(self, agent_type: str, node: str) -> tuple[uuid.UUID, float]:
        self.executions.append({"agent": agent_type, "node": node, "status": "running"})
        return uuid.uuid4(), 0.0

    async def finish_execution(
        self,
        execution: tuple[uuid.UUID, float],
        *,
        status: str,
        call: NodeCall,
        error: str | None = None,
    ) -> int:
        running = next(e for e in reversed(self.executions) if e["status"] == "running")
        running.update(status=status, tokens=call.usage.prompt_tokens, error=error)
        return 5


def _config() -> Any:
    return {"configurable": {"thread_id": str(uuid.uuid4())}}


async def test_full_run_produces_honest_report() -> None:
    fake = FakeLLM()
    rt = MemoryRuntime(fake)
    graph = build_graph(rt, InMemorySaver())
    config = _config()
    state = await graph.ainvoke(
        {"request": "payroll is slow", "execution_mode": "assisted"}, config
    )

    report = state["report"]
    assert report["request_type"] == "incident_investigation"
    assert report["classification_verified"] is False
    assert report["evidence"] == [] and report["evidence_sufficient"] is False
    assert report["root_cause"] is None
    assert "No diagnostic evidence" in report["conclusion"]
    # unknown agent dropped, display-name agent normalised, knowledge added by coverage
    assert [t["agent"] for t in report["tasks"]] == [
        "infrastructure_diagnostics",
        "application_diagnostics",
        "knowledge",
    ]
    assert all(t["status"] == "skipped" for t in report["tasks"])
    # nothing resolved in the inventory: no tool ran and nothing was guessed
    assert rt.tool_runs == []

    types = [e[0] for e in rt.events]
    for expected in (
        "supervisor.classified",
        "supervisor.plan_created",
        "rca.generated",
        "report.generated",
    ):
        assert expected in types
    assert sorted(fake.calls) == ["classify", "plan", "tools", "tools"]
    assert {e["status"] for e in rt.executions} == {"success"}
    assert rt.executions[0]["tokens"] == 120


async def test_diagnostics_collect_evidence_and_rca_is_guarded() -> None:
    rid = str(uuid.uuid4())
    fake = FakeLLM(
        classifications=[{**CLASSIFICATION, "affected_entities": ["app-01"]}],
        plan={"tasks": [{"agent": "infrastructure_diagnostics", "objective": "Check CPU"}]},
        tool_script={
            "infrastructure_diagnostics": [
                [("linux_system_snapshot", {"resource_id": rid})],
                [("linux_top_processes", {"resource_id": rid}), ("rm_rf", {})],
            ]
        },
        rca_statement="worker.py saturates the CPU",
    )
    rt = MemoryRuntime(fake)
    rt.inventory["app-01"] = {
        "name": "app-01",
        "resource_id": rid,
        "resource_type": "linux_server",
        "health": "degraded",
    }
    rt.tool_outputs["linux_top_processes"] = (
        "Top process: python worker.py (97% CPU)",
        "PID 4242 97%",
    )
    graph = build_graph(rt, InMemorySaver())
    state = await graph.ainvoke(
        {
            "request": "Why is CPU high on app-01?",
            "execution_mode": "assisted",
            "allow_remote": True,
        },
        _config(),
    )
    report = state["report"]
    agents = [t["agent"] for t in report["tasks"]]
    assert agents == ["infrastructure_diagnostics", "discovery", "knowledge"]
    diag = [e for e in report["evidence"] if e["source"] == "tool"]
    assert [e["tool"] for e in diag] == ["linux_system_snapshot", "linux_top_processes"]
    assert [e["id"] for e in diag] == ["E1.1", "E1.2"]
    assert [r[1] for r in rt.tool_runs] == ["linux_system_snapshot", "linux_top_processes", "rm_rf"]
    root = report["root_cause"]
    assert root["status"] == "confirmed" and root["statement"] == "worker.py saturates the CPU"
    assert set(root["evidence_ids"]) >= {"E1.1", "E1.2"}
    assert report["evidence_sufficient"] is True
    assert report["conclusion"].startswith("Root cause (confirmed")
    kinds = {f["kind"] for f in report["findings"]}
    assert "confirmed_fact" in kinds
    types = [e[0] for e in rt.events]
    assert types.count("tool.completed") == 2 and "tool.failed" in types
    assert "finding.recorded" in types
    assert fake.calls.count("findings") == 1 and fake.calls.count("rca") == 1


async def test_rca_downgrades_unsupported_confirmation() -> None:
    rid = str(uuid.uuid4())
    fake = FakeLLM(
        classifications=[{**CLASSIFICATION, "affected_entities": ["app-01"]}],
        plan={"tasks": []},
        rca={
            "root_cause": {
                "statement": "Cosmic rays",
                "status": "confirmed",
                "confidence": 0.95,
                "evidence_ids": ["E9.9"],
            },
        },
    )
    rt = MemoryRuntime(fake)
    rt.inventory["app-01"] = {
        "name": "app-01",
        "resource_id": rid,
        "resource_type": "linux_server",
        "health": "unhealthy",
    }
    graph = build_graph(rt, InMemorySaver())
    state = await graph.ainvoke(
        {"request": "app-01 is down", "execution_mode": "assisted"}, _config()
    )
    report = state["report"]
    # the claimed cause cites nothing known: it is never presented as the root cause
    assert report["root_cause"] is None
    assert report["alternative_hypotheses"][0]["statement"] == "Cosmic rays"
    assert report["evidence_sufficient"] is False
    assert "do not establish a root cause" in report["conclusion"]


async def test_json_tool_protocol_when_tools_are_rejected() -> None:
    rid = str(uuid.uuid4())
    fake = FakeLLM(
        classifications=[{**CLASSIFICATION, "affected_entities": ["app-01"]}],
        plan={"tasks": [{"agent": "infrastructure_diagnostics", "objective": "Check CPU"}]},
        tool_script={
            "infrastructure_diagnostics": [[("linux_top_processes", {"resource_id": rid})]]
        },
        native_tools=False,
    )
    rt = MemoryRuntime(fake)
    rt.inventory["app-01"] = {
        "name": "app-01",
        "resource_id": rid,
        "resource_type": "linux_server",
        "health": "degraded",
    }
    graph = build_graph(rt, InMemorySaver())
    state = await graph.ainvoke(
        {"request": "CPU high on app-01", "execution_mode": "assisted"}, _config()
    )
    assert [r[1] for r in rt.tool_runs] == ["linux_top_processes"]
    assert state["report"]["root_cause"]["status"] == "probable"  # one tool item is not proof


def test_coverage_adds_missing_specialists() -> None:
    resources: list[dict[str, object]] = [
        {"resolved": True, "name": "web", "resource_type": "docker_container"},
        {"resolved": True, "name": "app-01", "resource_type": "linux_server"},
    ]
    tasks = ensure_coverage([], resources, "incident_investigation", triage=True)
    assert [t.agent for t in tasks] == [
        "incident_triage",
        "discovery",
        "application_diagnostics",
        "infrastructure_diagnostics",
        "knowledge",
    ]
    change = ensure_coverage([], resources, "change_impact")
    assert [t.agent for t in change] == ["discovery", "change_impact"]


async def test_clarification_interrupt_and_resume() -> None:
    fake = FakeLLM(classifications=[VAGUE, {**VAGUE, "needs_clarification": False}])
    rt = MemoryRuntime(fake)
    saver = InMemorySaver()
    config = _config()
    graph = build_graph(rt, saver)
    await graph.ainvoke({"request": "it is broken", "execution_mode": "assisted"}, config)
    snapshot = await graph.aget_state(config)
    assert snapshot.interrupts
    assert snapshot.interrupts[0].value == {"question": "Which system is affected?"}

    # A new graph instance (as after a restart) continues from the checkpoint.
    graph2 = build_graph(rt, saver)
    state = await graph2.ainvoke(Command(resume="the payroll API"), config)
    assert state["clarifications"] == [
        {"question": "Which system is affected?", "answer": "the payroll API"}
    ]
    assert state["report"]["clarifications"][0]["answer"] == "the payroll API"
    assert fake.calls.count("classify") == 2


async def test_clarification_limit() -> None:
    fake = FakeLLM(classifications=[VAGUE])  # always vague
    rt = MemoryRuntime(fake)
    saver = InMemorySaver()
    config = _config()
    graph = build_graph(rt, saver, max_clarifications=1)
    await graph.ainvoke({"request": "help", "execution_mode": "assisted"}, config)
    state = await graph.ainvoke(Command(resume="no idea"), config)
    assert "report" in state  # proceeds after the limit instead of asking forever


async def test_cancellation_stops_at_node_boundary() -> None:
    fake = FakeLLM()
    rt = MemoryRuntime(fake)

    def cancel_after_classify(kind: str) -> None:
        if kind == "classify":
            rt.cancelled = True

    fake.before_reply = cancel_after_classify
    graph = build_graph(rt, InMemorySaver())
    with pytest.raises(InvestigationCancelledError):
        await graph.ainvoke({"request": "x y z", "execution_mode": "assisted"}, _config())
    assert fake.calls == ["classify"]


async def test_model_failure_marks_agent_failed() -> None:
    fake = FakeLLM(fail_status=400)
    rt = MemoryRuntime(fake)
    graph = build_graph(rt, InMemorySaver())
    with pytest.raises(Exception, match="rejected"):
        await graph.ainvoke({"request": "x y z", "execution_mode": "assisted"}, _config())
    assert rt.events[-1][0] == "agent.failed"
    assert rt.executions[-1]["status"] == "failed"


def test_sanitise_plan_limits_and_dedupes() -> None:
    plan = TaskPlan(
        tasks=[PlannedTask(agent="network-diagnostics", objective=f"o{i % 2}") for i in range(10)]
        + [PlannedTask(agent="supervisor", objective="not a specialist")]
    )
    tasks = sanitise_plan(plan)
    assert [(t.agent, t.objective) for t in tasks] == [
        ("network_diagnostics", "o0"),
        ("network_diagnostics", "o1"),
    ]


def test_classification_is_lenient() -> None:
    from app.agents.supervisor import Classification, TaskPlan

    c = Classification.model_validate(
        {
            "request_type": "Incident Investigation",
            "urgency": "HIGH",
            "affected_entities": "web-01, db-01",
            "symptoms": None,
            "needs_clarification": "false",
            "confidence": "85%",
        }
    )
    assert c.request_type == "incident_investigation"
    assert c.urgency == "high"
    assert c.affected_entities == ["web-01", "db-01"]
    assert c.symptoms == []
    assert c.needs_clarification is False
    assert c.confidence == pytest.approx(0.85)
    assert c.summary
    assert Classification.model_validate({"request_type": "outage"}).request_type == (
        "incident_investigation"
    )
    plan = TaskPlan.model_validate([{"agent": "x", "objective": "y"}])
    assert len(plan.tasks) == 1
