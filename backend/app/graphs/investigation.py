"""Incident investigation workflow (spec §11.4, Workflow 5.4).

    classify ──► clarify? (interrupt, ≤N times) ──► resolve_resources ──► plan_tasks
        ──► run_agent (one per task, fan-out via Send) ──► aggregate ──► report

Entities named in the request are resolved against the resource inventory. The
Discovery agent (no LLM) turns resolved resources into inventory evidence: health,
key facts and dependencies. Other specialists land in later phases; their tasks are
reported as *skipped*. Nothing here fabricates diagnostic findings.

State holds JSON-compatible values only so checkpoints stay portable.
"""

from __future__ import annotations

import operator
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send, interrupt

from app.agents import supervisor
from app.agents.catalogue import get_spec
from app.graphs.runtime import AgentUnavailableError, InvestigationRuntime, NodeCall

WORKFLOW_ID = "incident_investigation.v1"
DEFAULT_QUESTION = "Which system, service or symptom should I investigate?"


class InvestigationState(TypedDict, total=False):
    request: str
    execution_mode: str
    classification: dict[str, Any]
    clarifications: list[dict[str, str]]
    resources: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    findings: Annotated[list[dict[str, Any]], operator.add]
    aggregate: dict[str, Any]
    report: dict[str, Any]


NodeFn = Callable[[Any, NodeCall], Awaitable[dict[str, Any]]]


def build_graph(
    rt: InvestigationRuntime,
    checkpointer: BaseCheckpointSaver[Any] | None,
    *,
    max_clarifications: int = 2,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    def instrumented(agent_type: str, node: str, fn: NodeFn) -> Callable[[Any], Any]:
        """Wrap a node with cancellation checks, events and execution records."""

        async def wrapper(state: Any) -> dict[str, Any]:
            await rt.check_cancelled()
            execution = await rt.start_execution(agent_type, node)
            await rt.emit("agent.started", agent_type, {"node": node})
            call = NodeCall()
            try:
                update = await fn(state, call)
            except GraphBubbleUp:
                await rt.finish_execution(execution, status="interrupted", call=call)
                raise
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                duration = await rt.finish_execution(
                    execution, status="failed", call=call, error=message
                )
                await rt.emit(
                    "agent.failed",
                    agent_type,
                    {"node": node, "error": message[:500], "duration_ms": duration},
                )
                raise
            duration = await rt.finish_execution(execution, status="success", call=call)
            await rt.emit(
                "agent.completed",
                agent_type,
                {
                    "node": node,
                    "duration_ms": duration,
                    "llm_calls": call.usage.calls,
                    "prompt_tokens": call.usage.prompt_tokens,
                    "completion_tokens": call.usage.completion_tokens,
                    "model": call.model_name,
                },
            )
            return update

        return wrapper

    # ------------------------------------------------------------------ nodes

    async def classify(state: InvestigationState, call: NodeCall) -> dict[str, Any]:
        binding = await rt.bind_agent("supervisor")
        if not binding.enabled:
            raise AgentUnavailableError("The Supervisor agent is disabled")
        result = await rt.structured(
            call,
            binding,
            supervisor.classify_messages(state["request"], state.get("clarifications", [])),
            supervisor.Classification,
            example=supervisor.CLASSIFICATION_EXAMPLE,
        )
        data = result.model_dump()
        await rt.emit("supervisor.classified", "supervisor", data)
        return {"classification": data}

    async def clarify(state: InvestigationState) -> dict[str, Any]:
        # Re-executed from the top on resume; interrupt() then returns the answer.
        question = (state["classification"].get("clarification_question") or "").strip()
        answer = interrupt({"question": question or DEFAULT_QUESTION})
        return {
            "clarifications": [
                *state.get("clarifications", []),
                {"question": question or DEFAULT_QUESTION, "answer": str(answer).strip()[:2000]},
            ]
        }

    async def resolve_resources(state: InvestigationState, call: NodeCall) -> dict[str, Any]:
        # Entities are matched against the inventory; unmatched ones stay unverified
        # (with candidates where the match was ambiguous) rather than guessed.
        entities = state["classification"].get("affected_entities", [])
        resources = await rt.resolve_entities(entities)
        matched = [r["resource_id"] for r in resources if r.get("resolved")]
        if matched:
            await rt.set_resource_ids(matched)
        await rt.emit(
            "resources.resolved",
            "supervisor",
            {
                "resolved": len(matched),
                "unresolved": len(resources) - len(matched),
                "entities": entities,
                "resources": resources,
            },
        )
        return {"resources": resources}

    async def plan_tasks(state: InvestigationState, call: NodeCall) -> dict[str, Any]:
        binding = await rt.bind_agent("supervisor")
        classification = supervisor.Classification.model_validate(state["classification"])
        plan = await rt.structured(
            call,
            binding,
            supervisor.plan_messages(state["request"], classification, state.get("resources", [])),
            supervisor.TaskPlan,
            example=supervisor.PLAN_EXAMPLE,
        )
        tasks = [
            {"id": f"task-{i + 1}", **t.model_dump()}
            for i, t in enumerate(supervisor.sanitise_plan(plan))
        ]
        await rt.emit("supervisor.plan_created", "supervisor", {"tasks": tasks})
        return {"tasks": tasks}

    async def run_agent(payload: dict[str, Any]) -> dict[str, Any]:
        await rt.check_cancelled()
        task = payload["task"]
        spec = get_spec(task["agent"])
        binding = await rt.bind_agent(task["agent"])
        if spec is None or not spec.implemented:
            phase = spec.available_in_phase if spec else None
            reason = f"Agent not available yet (planned for phase {phase})"
        elif not binding.enabled:
            reason = "Agent is disabled"
        elif task["agent"] == "discovery":
            return await run_discovery_task(task, payload.get("resources") or [])
        else:  # pragma: no cover - every implemented specialist has an executor
            reason = "Agent has no executor registered"
        await rt.emit(
            "agent.skipped",
            task["agent"],
            {"task_id": task["id"], "objective": task["objective"], "reason": reason},
        )
        return {
            "findings": [
                {
                    "task_id": task["id"],
                    "agent": task["agent"],
                    "status": "skipped",
                    "reason": reason,
                    "evidence": [],
                }
            ]
        }

    async def run_discovery_task(
        task: dict[str, Any], resources: list[dict[str, Any]]
    ) -> dict[str, Any]:
        execution = await rt.start_execution("discovery", "run_agent")
        await rt.emit("agent.started", "discovery", {"node": "run_agent", "task_id": task["id"]})
        call = NodeCall()
        ids = [uuid.UUID(r["resource_id"]) for r in resources if r.get("resolved")]
        try:
            evidence = await rt.inventory_evidence(ids)
        except Exception as exc:
            await rt.finish_execution(execution, status="failed", call=call, error=str(exc))
            raise
        duration = await rt.finish_execution(execution, status="success", call=call)
        status = "completed" if evidence else "skipped"
        reason = None if evidence else "No affected entity matched a resource in the inventory"
        await rt.emit(
            "agent.completed",
            "discovery",
            {
                "node": "run_agent",
                "task_id": task["id"],
                "duration_ms": duration,
                "evidence_items": len(evidence),
                "llm_calls": 0,
            },
        )
        return {
            "findings": [
                {
                    "task_id": task["id"],
                    "agent": "discovery",
                    "status": status,
                    "reason": reason,
                    "evidence": evidence,
                }
            ]
        }

    async def aggregate(state: InvestigationState, call: NodeCall) -> dict[str, Any]:
        findings = state.get("findings", [])
        evidence = [e for f in findings for e in f.get("evidence", [])]
        summary = {
            "tasks": len(state.get("tasks", [])),
            "completed": sum(1 for f in findings if f["status"] == "completed"),
            "skipped": sum(1 for f in findings if f["status"] == "skipped"),
            "evidence_items": len(evidence),
            "sufficient": bool(evidence),
        }
        await rt.emit("evidence.aggregated", "supervisor", summary)
        return {"aggregate": summary}

    async def report(state: InvestigationState, call: NodeCall) -> dict[str, Any]:
        result = build_report(state)
        await rt.emit(
            "report.generated",
            "supervisor",
            {"conclusion": result["conclusion"], "evidence_sufficient": False},
        )
        return {"report": result}

    # ---------------------------------------------------------------- routing

    def after_classify(state: InvestigationState) -> str:
        asked = len(state.get("clarifications", []))
        if state["classification"].get("needs_clarification") and asked < max_clarifications:
            return "clarify"
        return "resolve_resources"

    def fan_out(state: InvestigationState) -> list[Send] | str:
        tasks = state.get("tasks", [])
        if not tasks:
            return "aggregate"
        resources = state.get("resources", [])
        return [Send("run_agent", {"task": t, "resources": resources}) for t in tasks]

    # Node callables are wrapped dynamically; langgraph's overloads cannot type them.
    graph: Any = StateGraph(InvestigationState)
    graph.add_node("classify", instrumented("supervisor", "classify", classify))
    graph.add_node("clarify", clarify)
    graph.add_node(
        "resolve_resources", instrumented("supervisor", "resolve_resources", resolve_resources)
    )
    graph.add_node("plan_tasks", instrumented("supervisor", "plan_tasks", plan_tasks))
    graph.add_node("run_agent", run_agent)
    graph.add_node("aggregate", instrumented("supervisor", "aggregate", aggregate))
    graph.add_node("report", instrumented("supervisor", "report", report))

    graph.add_edge(START, "classify")
    graph.add_conditional_edges("classify", after_classify, ["clarify", "resolve_resources"])
    graph.add_edge("clarify", "classify")
    graph.add_edge("resolve_resources", "plan_tasks")
    graph.add_conditional_edges("plan_tasks", fan_out, ["run_agent", "aggregate"])
    graph.add_edge("run_agent", "aggregate")
    graph.add_edge("aggregate", "report")
    graph.add_edge("report", END)
    compiled: CompiledStateGraph[Any, Any, Any, Any] = graph.compile(checkpointer=checkpointer)
    return compiled


def build_report(state: InvestigationState) -> dict[str, Any]:
    """Deterministic report: states what is known, and what is not."""
    c = state.get("classification", {})
    findings = state.get("findings", [])
    tasks = state.get("tasks", [])
    by_task = {f["task_id"]: f for f in findings}
    evidence = [e for f in findings for e in f.get("evidence", [])]
    task_rows = [
        {
            **t,
            "status": by_task.get(t["id"], {}).get("status", "not_run"),
            "reason": by_task.get(t["id"], {}).get("reason"),
        }
        for t in tasks
    ]
    if evidence:
        unwell = [e["resource"] for e in evidence if e.get("health") in ("unhealthy", "degraded")]
        conclusion = (
            f"Inventory evidence was collected for {len(evidence)} resource(s)"
            + (f"; not healthy: {', '.join(unwell)}" if unwell else "; all reported healthy")
            + ". It reflects the last discovered state, not live diagnostics, so no root "
            "cause is stated yet."
        )
    else:
        conclusion = (
            "No diagnostic evidence was collected, so no root cause can be stated. "
            "The request was classified and an investigation plan was produced; the "
            "specialist agents that gather evidence are not available yet."
        )
    # Completed tasks are reported as evidence; only the rest remain as next steps.
    next_steps = [
        f"{t['agent'].replace('_', ' ').title()}: {t['objective']}"
        for t in task_rows
        if t["status"] != "completed"
    ]
    if not next_steps:
        next_steps = (
            ["Review the inventory evidence above and the affected resources' dependencies."]
            if evidence
            else ["Refine the request with a specific system, service or symptom."]
        )
    return {
        "workflow": WORKFLOW_ID,
        "summary": c.get("summary", ""),
        "request_type": c.get("request_type"),
        "urgency": c.get("urgency"),
        "classification_confidence": c.get("confidence"),
        "classification_verified": bool(state.get("resources"))
        and all(r.get("resolved") for r in state.get("resources", [])),
        "affected_entities": c.get("affected_entities", []),
        "symptoms": c.get("symptoms", []),
        "resources": state.get("resources", []),
        "clarifications": state.get("clarifications", []),
        "tasks": task_rows,
        "evidence": evidence,
        "evidence_sufficient": bool(evidence),
        "root_cause": None,
        "conclusion": conclusion,
        "recommended_next_steps": next_steps,
    }
