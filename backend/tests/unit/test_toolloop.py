"""The bounded tool loop: validation feedback, limits, citations and the baseline net."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.agents import toolloop as tl
from app.llm.gateway import ChatMessage, Usage
from app.llm.tool_calling import ToolCall, ToolExchange, ToolTurn
from app.tools.executor import ToolRunResult
from app.tools.registry import tools_for_agent

RID = str(uuid.uuid4())
RESOURCES = [
    {
        "resolved": True,
        "resource_id": RID,
        "name": "app-01",
        "resource_type": "linux_server",
        "health": "degraded",
    }
]


def _turn(*calls: tuple[str, dict[str, Any]] | ToolCall, content: str = "") -> ToolTurn:
    built = [
        c if isinstance(c, ToolCall) else ToolCall(f"c{i}", c[0], c[1]) for i, c in enumerate(calls)
    ]
    return ToolTurn(built, content, Usage(), "native")


class Script:
    def __init__(self, turns: list[ToolTurn], findings: Any = None) -> None:
        self.turns = turns
        self.findings = findings
        self.runs: list[tuple[str, dict[str, Any], str]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.histories: list[list[ToolExchange]] = []

    async def chat(
        self, messages: list[ChatMessage], tools: Any, history: list[ToolExchange]
    ) -> ToolTurn:
        self.histories.append(list(history))
        return self.turns.pop(0) if self.turns else _turn(content="done")

    async def run(self, name: str, args: dict[str, Any], eid: str) -> ToolRunResult:
        self.runs.append((name, args, eid))
        if name == "nope":
            return ToolRunResult(name, "rejected", "Rejected", "ERROR: Unknown tool 'nope'")
        if name == "linux_journal":
            return ToolRunResult(
                name, "failed", "linux_journal failed: denied", "ERROR (permission): denied",
                resource_id=RID, resource_name="app-01",
            )  # fmt: skip
        return ToolRunResult(
            name,
            "success",
            f"{name} ok",
            f"output of {name}",
            arguments=args,
            resource_id=RID,
            resource_name="app-01",
            resource_type="linux_server",
        )

    async def finish(self, messages: list[ChatMessage]) -> tl.AgentFindings:
        if isinstance(self.findings, Exception):
            raise self.findings
        return tl.AgentFindings.model_validate(self.findings or {"summary": "s"})

    async def emit(self, event: str, data: dict[str, Any]) -> None:
        self.events.append((event, data))


def _spec(**kw: Any) -> tl.LoopSpec:
    return tl.LoopSpec(
        **{
            "agent_type": "infrastructure_diagnostics",
            "agent_name": "Infrastructure Diagnostics Agent",
            "objective": "Why is CPU high",
            "request": "Why is CPU high on app-01?",
            "resources": RESOURCES,
            "tools": tools_for_agent("infrastructure_diagnostics"),
            "evidence_prefix": "E1",
            **kw,
        }
    )


def _deps(s: Script, budget: int = 30) -> tl.LoopDeps:
    return tl.LoopDeps(s.chat, s.run, s.finish, s.emit, tl.ToolBudget(budget))


async def test_rejections_are_fed_back_and_evidence_is_numbered() -> None:
    s = Script(
        [
            _turn(("nope", {}), ToolCall("x", "linux_top_processes", {}, "arguments are not valid JSON")),
            _turn(("linux_top_processes", {"resource_id": RID}), ("linux_journal", {"resource_id": RID})),
            _turn(("linux_top_processes", {"resource_id": RID})),  # a repeat is not re-run
            _turn(content="A python process saturates the CPU."),
        ],
        findings={
            "summary": "CPU saturated",
            "observations": [
                {"statement": "python uses 97% CPU", "kind": "confirmed_fact", "evidence_ids": ["E1.1"]},
                {"statement": "journal unreadable", "kind": "confirmed_fact", "evidence_ids": ["E1.2"]},
                {"statement": "disk is full", "kind": "confirmed_fact", "evidence_ids": ["E9.9"]},
            ],
            "hypotheses": [{"statement": "busy loop", "likelihood": "high", "evidence_ids": "E1.1"}],
        },
    )  # fmt: skip
    budget = tl.ToolBudget(30)
    deps = tl.LoopDeps(s.chat, s.run, s.finish, s.emit, budget)
    result = await tl.run_tool_loop(_spec(), deps)

    first_results = s.histories[1][0].results
    assert first_results[0].startswith("ERROR: Unknown tool")
    assert "not valid JSON" in first_results[1]
    assert s.histories[3][2].results[0].startswith("(already collected) [E1.1]")
    assert [e["id"] for e in result.evidence] == ["E1.1", "E1.2"]
    assert [e["ok"] for e in result.evidence] == [True, False]
    assert budget.remaining == 28  # the rejected call gave its slot back
    assert result.rejected == 2 and result.final_text.startswith("A python")

    obs = {o.statement: o for o in result.findings.observations}
    assert obs["python uses 97% CPU"].kind == "confirmed_fact"
    assert obs["journal unreadable"].kind == "observation"  # failed output is not a fact
    assert "disk is full" not in obs  # cites nothing known → unverified hypothesis
    downgraded = [h for h in result.findings.hypotheses if h.statement == "disk is full"]
    assert downgraded[0].likelihood == "low" and downgraded[0].evidence_ids == []
    assert [e for e, _ in s.events].count("tool.completed") == 1
    assert ("tool.failed", "rejected") in [(e, d.get("status")) for e, d in s.events]

    finding = result.as_finding("task-1", "infrastructure_diagnostics")
    assert finding["status"] == "completed" and finding["tool_calls"] == 3


async def test_step_limit_and_budget() -> None:
    endless = [
        _turn(("linux_top_processes", {"resource_id": RID, "limit": 3 + i})) for i in range(10)
    ]
    s = Script(endless)
    result = await tl.run_tool_loop(_spec(max_steps=3), _deps(s))
    assert result.steps == 3 and len(result.evidence) == 3
    assert any("step limit" in n for n in result.notes)

    s = Script(
        [_turn(*[("linux_top_processes", {"resource_id": RID, "limit": 3 + i}) for i in range(4)])]
    )
    result = await tl.run_tool_loop(_spec(), _deps(s, budget=2))
    assert len(result.evidence) == 2
    assert any("budget" in n for n in result.notes)
    assert s.histories == [[]]  # stopped asking the model once the budget ran out


async def test_baseline_runs_when_the_model_collects_nothing() -> None:
    s = Script([_turn(content="Everything looks fine to me.")], findings=RuntimeError("bad model"))
    result = await tl.run_tool_loop(_spec(), _deps(s))
    assert [r[0] for r in s.runs] == ["linux_system_snapshot", "linux_top_processes"]
    assert all(e["baseline"] for e in result.evidence)
    assert any("baseline" in n for n in result.notes)
    # the findings model failed: deterministic fallback from tool summaries
    assert [o.evidence_ids for o in result.findings.observations] == [["E1.1"], ["E1.2"]]


async def test_no_remote_permission_skips_remote_baseline() -> None:
    s = Script([_turn(content="nothing")])
    result = await tl.run_tool_loop(_spec(allow_remote=False), _deps(s))
    assert s.runs == [] and result.evidence == []
    assert result.as_finding("t", "a")["status"] == "skipped"


@pytest.mark.parametrize(
    "raw",
    [
        {"findings": ["CPU 97% (E1.1)"], "hypotheses": None},
        {"observations": [{"finding": "CPU 97%", "evidence": [{"id": "E1.1"}], "kind": "Fact"}]},
    ],
)
def test_findings_are_parsed_leniently(raw: dict[str, Any]) -> None:
    f = tl.AgentFindings.model_validate(raw)
    assert f.observations[0].evidence_ids == ["E1.1"]


def test_evidence_digest_is_bounded() -> None:
    ev = [
        {"id": f"E1.{i}", "tool": "t", "resource": "r", "summary": "s", "excerpt": "x" * 5000}
        for i in range(1, 30)
    ]
    digest = tl.evidence_digest(ev)
    assert len(digest) < tl.FINDINGS_PROMPT_CHARS + 3000
    assert "[E1.29]" in digest
