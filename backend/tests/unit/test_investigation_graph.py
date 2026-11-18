"""Investigation graph behaviour with an in-memory runtime and checkpointer."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.catalogue import get_spec
from app.agents.supervisor import PlannedTask, TaskPlan, sanitise_plan
from app.core.config import get_settings
from app.graphs.investigation import build_graph
from app.graphs.runtime import (
    AgentBinding,
    InvestigationCancelledError,
    InvestigationRuntime,
    NodeCall,
)
from app.llm.gateway import EndpointConfig, ModelClient
from tests.fakes import VAGUE, FakeLLM


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
    # unknown agent dropped, display-name agent normalised
    assert [t["agent"] for t in report["tasks"]] == [
        "infrastructure_diagnostics",
        "application_diagnostics",
    ]
    assert all(t["status"] == "skipped" for t in report["tasks"])
    assert "phase 4" in report["tasks"][0]["reason"]

    types = [e[0] for e in rt.events]
    assert types.count("agent.skipped") == 2
    for expected in ("supervisor.classified", "supervisor.plan_created", "report.generated"):
        assert expected in types
    assert fake.calls == ["classify", "plan"]
    assert {e["status"] for e in rt.executions} == {"success"}
    assert rt.executions[0]["tokens"] == 120


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
