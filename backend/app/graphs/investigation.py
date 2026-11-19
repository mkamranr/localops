"""Incident investigation workflow (spec §11.4, Workflow 5.4).

    classify ──► clarify? (interrupt, ≤N times) ──► resolve_resources ──► plan_tasks
        ──► run_agent (one per task, fan-out via Send) ──► aggregate
        ──► analyse_root_cause ──► report

Entities named in the request (or resources chosen explicitly) are resolved against
the inventory. Specialists then gather evidence:

* Discovery: inventory evidence (health, facts, dependencies), no model.
* Infrastructure / Application / Network Diagnostics: a bounded loop in which the
  model chooses read-only tools; every executed call becomes cited evidence.
* Knowledge, Incident Triage, Change Impact: deterministic answers from LocalOps' own
  records.

The Root Cause Analysis agent correlates the evidence; deterministic guards decide
whether a root cause may be called confirmed, probable or only unverified. Nothing
here changes a system, and nothing is presented as fact without evidence.

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

from app.agents import assessments, rca, supervisor
from app.agents.catalogue import get_spec
from app.agents.diagnostics import ROLES, TOOL_LOOP_AGENTS
from app.agents.toolloop import AgentFindings, LoopDeps, LoopSpec, run_tool_loop
from app.graphs.runtime import AgentUnavailableError, InvestigationRuntime, NodeCall
from app.llm.gateway import ModelError
from app.tools.registry import tools_for_agent

WORKFLOW_ID = "incident_investigation.v1"
DEFAULT_QUESTION = "Which system, service or symptom should I investigate?"


class InvestigationState(TypedDict, total=False):
    request: str
    execution_mode: str
    # context fixed when the investigation is created
    explicit_resource_ids: list[str]
    incident_id: str | None
    allow_remote: bool
    classification: dict[str, Any]
    clarifications: list[dict[str, str]]
    resources: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    findings: Annotated[list[dict[str, Any]], operator.add]
    aggregate: dict[str, Any]
    rca: dict[str, Any]
    report: dict[str, Any]


NodeFn = Callable[[Any, NodeCall], Awaitable[dict[str, Any]]]
AgentFn = Callable[[dict[str, Any], dict[str, Any], NodeCall], Awaitable[dict[str, Any]]]


def evidence_prefix(task_id: str) -> str:
    """``task-3`` → ``E3``; evidence ids are ``E3.1``, ``E3.2`` …"""
    suffix = task_id.rsplit("-", 1)[-1]
    return f"E{suffix}" if suffix.isdigit() else f"E{abs(hash(task_id)) % 1000}"


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
        # Resources selected explicitly (or linked to the incident) always count.
        entities = state["classification"].get("affected_entities", [])
        explicit = await rt.resources_by_ids(state.get("explicit_resource_ids") or [])
        found = await rt.resolve_entities(entities)
        seen = {r["resource_id"] for r in explicit}
        resources = list(explicit)
        for r in found:
            if r.get("resolved") and r["resource_id"] in seen:
                continue
            if r.get("resolved"):
                seen.add(r["resource_id"])
            resources.append(r)
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
        resources = state.get("resources", [])
        plan = await rt.structured(
            call,
            binding,
            supervisor.plan_messages(state["request"], classification, resources),
            supervisor.TaskPlan,
            example=supervisor.PLAN_EXAMPLE,
        )
        planned = supervisor.ensure_coverage(
            supervisor.sanitise_plan(plan),
            resources,
            classification.request_type,
            triage=bool(state.get("incident_id")),
        )
        tasks = [{"id": f"task-{i + 1}", **t.model_dump()} for i, t in enumerate(planned)]
        await rt.emit("supervisor.plan_created", "supervisor", {"tasks": tasks})
        return {"tasks": tasks}

    # ------------------------------------------------------------- specialists

    def skipped(task: dict[str, Any], reason: str) -> dict[str, Any]:
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

    async def run_agent(payload: dict[str, Any]) -> dict[str, Any]:
        await rt.check_cancelled()
        task = payload["task"]
        agent = task["agent"]
        spec = get_spec(agent)
        binding = await rt.bind_agent(agent)
        executor = executors.get(agent)
        if spec is None or not spec.implemented:
            phase = spec.available_in_phase if spec else None
            reason = f"Agent not available yet (planned for phase {phase})"
        elif not binding.enabled:
            reason = "Agent is disabled"
        elif executor is None:  # pragma: no cover - every implemented specialist has one
            reason = "Agent has no executor registered"
        else:
            return await specialist(agent, task, payload, executor)
        await rt.emit(
            "agent.skipped",
            agent,
            {"task_id": task["id"], "objective": task["objective"], "reason": reason},
        )
        return skipped(task, reason)

    async def specialist(
        agent: str, task: dict[str, Any], payload: dict[str, Any], fn: AgentFn
    ) -> dict[str, Any]:
        execution = await rt.start_execution(agent, "run_agent")
        await rt.emit(
            "agent.started",
            agent,
            {"node": "run_agent", "task_id": task["id"], "objective": task["objective"]},
        )
        call = NodeCall()
        try:
            finding = await fn(task, payload, call)
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            duration = await rt.finish_execution(
                execution, status="failed", call=call, error=message
            )
            await rt.emit(
                "agent.failed",
                agent,
                {
                    "node": "run_agent",
                    "task_id": task["id"],
                    "error": message[:500],
                    "duration_ms": duration,
                },
            )
            if isinstance(exc, (AgentUnavailableError, ModelError)):
                # One specialist failing must not sink the investigation.
                return {
                    "findings": [
                        {
                            "task_id": task["id"],
                            "agent": agent,
                            "status": "failed",
                            "reason": message[:500],
                            "evidence": [],
                        }
                    ]
                }
            raise
        for kind, items in (
            ("observation", finding.get("observations") or []),
            ("hypothesis", finding.get("hypotheses") or []),
        ):
            for item in items:
                await rt.emit(
                    "finding.recorded",
                    agent,
                    {
                        "task_id": task["id"],
                        "kind": item.get("kind", kind),
                        "statement": item["statement"],
                        "likelihood": item.get("likelihood"),
                        "evidence_ids": item.get("evidence_ids", []),
                    },
                )
        duration = await rt.finish_execution(execution, status="success", call=call)
        await rt.emit(
            "agent.completed",
            agent,
            {
                "node": "run_agent",
                "task_id": task["id"],
                "duration_ms": duration,
                "evidence_items": len(finding.get("evidence", [])),
                "tool_calls": finding.get("tool_calls", 0),
                "status": finding["status"],
                "llm_calls": call.usage.calls,
                "prompt_tokens": call.usage.prompt_tokens,
                "completion_tokens": call.usage.completion_tokens,
                "model": call.model_name,
            },
        )
        return {"findings": [finding]}

    async def run_discovery(
        task: dict[str, Any], payload: dict[str, Any], call: NodeCall
    ) -> dict[str, Any]:
        resources = payload.get("resources") or []
        ids = [uuid.UUID(r["resource_id"]) for r in resources if r.get("resolved")]
        evidence = await rt.inventory_evidence(ids)
        prefix = evidence_prefix(task["id"])
        for i, e in enumerate(evidence, start=1):
            e["id"] = f"{prefix}.{i}"
        return {
            "task_id": task["id"],
            "agent": "discovery",
            "status": "completed" if evidence else "skipped",
            "reason": None
            if evidence
            else "No affected entity matched a resource in the inventory",
            "evidence": evidence,
            "summary": (f"Inventory evidence for {len(evidence)} resource(s)." if evidence else ""),
            "observations": [
                {"statement": e["summary"], "kind": "observation", "evidence_ids": [e["id"]]}
                for e in evidence
            ],
            "hypotheses": [],
        }

    async def run_diagnostics(
        task: dict[str, Any], payload: dict[str, Any], call: NodeCall
    ) -> dict[str, Any]:
        agent = task["agent"]
        binding = await rt.bind_agent(agent)
        tools = tools_for_agent(agent, binding.config.get("allowed_tools") or None)
        allow_remote = bool(payload.get("allow_remote", True))
        spec = LoopSpec(
            agent_type=agent,
            agent_name=binding.spec.name,
            objective=task["objective"],
            request=payload.get("request", ""),
            resources=payload.get("resources") or [],
            tools=tools,
            evidence_prefix=evidence_prefix(task["id"]),
            max_steps=int(binding.config.get("max_steps", 6)),
            role=ROLES.get(agent, ""),
            allow_remote=allow_remote,
        )
        budget = await rt.tool_budget()

        async def chat(messages: Any, schemas: Any, history: Any) -> Any:
            await rt.check_cancelled()
            return await rt.chat_tools(call, binding, messages, schemas, history)

        async def run(name: str, arguments: dict[str, Any], eid: str) -> Any:
            return await rt.run_tool(
                name,
                arguments,
                eid,
                agent_type=agent,
                allowed=tools,
                allow_remote=allow_remote,
            )

        async def finish(messages: Any) -> AgentFindings:
            return await rt.structured(
                call,
                binding,
                messages,
                AgentFindings,
                example=FINDINGS_EXAMPLE,
            )

        async def emit(event: str, data: dict[str, Any]) -> None:
            await rt.emit(event, agent, {"task_id": task["id"], **data})

        result = await run_tool_loop(spec, LoopDeps(chat, run, finish, emit, budget))
        if not allow_remote:
            result.notes.append(
                "Remote diagnostics were not permitted (the requester lacks "
                "tool.execute.readonly); only inventory tools ran"
            )
        return result.as_finding(task["id"], agent)

    async def run_knowledge(
        task: dict[str, Any], payload: dict[str, Any], call: NodeCall
    ) -> dict[str, Any]:
        text = " ".join(
            [payload.get("request", ""), *payload.get("symptoms", []), task["objective"]]
        )
        incident = payload.get("incident_id")
        result = await rt.in_session(
            lambda s: assessments.knowledge(
                s,
                text,
                payload.get("resources") or [],
                evidence_prefix(task["id"]),
                exclude_investigation=rt.investigation_id,
                exclude_incident=uuid.UUID(incident) if incident else None,
            )
        )
        return result.as_finding(task["id"], "knowledge")

    async def run_triage(
        task: dict[str, Any], payload: dict[str, Any], call: NodeCall
    ) -> dict[str, Any]:
        incident = payload.get("incident_id")
        result = await rt.in_session(
            lambda s: assessments.triage(
                s,
                payload.get("resources") or [],
                evidence_prefix(task["id"]),
                urgency=payload.get("urgency"),
                incident_id=uuid.UUID(incident) if incident else None,
            )
        )
        return result.as_finding(task["id"], "incident_triage")

    async def run_change_impact(
        task: dict[str, Any], payload: dict[str, Any], call: NodeCall
    ) -> dict[str, Any]:
        result = await rt.in_session(
            lambda s: assessments.change_impact(
                s, payload.get("resources") or [], evidence_prefix(task["id"])
            )
        )
        return result.as_finding(task["id"], "change_impact")

    executors: dict[str, AgentFn] = {
        "discovery": run_discovery,
        "knowledge": run_knowledge,
        "incident_triage": run_triage,
        "change_impact": run_change_impact,
        **dict.fromkeys(TOOL_LOOP_AGENTS, run_diagnostics),
    }

    # ---------------------------------------------------------------- analysis

    async def aggregate(state: InvestigationState, call: NodeCall) -> dict[str, Any]:
        findings = state.get("findings", [])
        evidence = [e for f in findings for e in f.get("evidence", [])]
        summary = {
            "tasks": len(state.get("tasks", [])),
            "completed": sum(1 for f in findings if f["status"] == "completed"),
            "skipped": sum(1 for f in findings if f["status"] == "skipped"),
            "failed": sum(1 for f in findings if f["status"] == "failed"),
            "evidence_items": len(evidence),
            "tool_evidence": sum(1 for e in evidence if e.get("source") == "tool"),
            "tool_calls": sum(int(f.get("tool_calls") or 0) for f in findings),
            "sufficient": bool(evidence),
        }
        await rt.emit("evidence.aggregated", "supervisor", summary)
        return {"aggregate": summary}

    async def analyse_root_cause(state: InvestigationState, call: NodeCall) -> dict[str, Any]:
        classification = state.get("classification", {})
        findings = state.get("findings", [])
        evidence = [e for f in findings for e in f.get("evidence", [])]
        if classification.get("request_type") == "change_impact":
            result = rca.RCAResult(incident_summary=classification.get("summary", ""))
            note = "Change impact request: no root-cause analysis needed"
        elif not evidence:
            result = rca.no_evidence_rca(classification.get("summary", ""))
            note = "No evidence collected"
        else:
            binding = await rt.bind_agent("root_cause_analysis")
            if not binding.enabled:
                result = rca.RCAResult(
                    incident_summary=classification.get("summary", ""),
                    evidence_gaps=["The Root Cause Analysis agent is disabled."],
                )
                note = "Agent disabled"
            else:
                try:
                    raw = await rt.structured(
                        call,
                        binding,
                        rca.rca_messages(state["request"], classification, evidence, findings),
                        rca.RCAResult,
                        example=rca.RCA_EXAMPLE,
                    )
                    result = rca.apply_guards(raw, evidence)
                    note = None
                except (ModelError, AgentUnavailableError) as exc:
                    result = rca.RCAResult(
                        incident_summary=classification.get("summary", ""),
                        evidence_gaps=[f"Root-cause analysis failed: {str(exc)[:200]}"],
                    )
                    note = "Model failure"
        data = result.model_dump()
        data["confidence_label"] = rca.confidence_label(result)
        await rt.emit(
            "rca.generated",
            "root_cause_analysis",
            {
                "root_cause": data["root_cause"],
                "confidence_label": data["confidence_label"],
                "contributing_factors": len(result.contributing_factors),
                "note": note,
            },
        )
        return {"rca": data}

    async def report(state: InvestigationState, call: NodeCall) -> dict[str, Any]:
        result = build_report(state)
        await rt.emit(
            "report.generated",
            "supervisor",
            {
                "conclusion": result["conclusion"],
                "evidence_sufficient": result["evidence_sufficient"],
            },
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
        c = state.get("classification", {})
        payload = {
            "resources": state.get("resources", []),
            "request": state["request"],
            "symptoms": c.get("symptoms", []),
            "urgency": c.get("urgency"),
            "incident_id": state.get("incident_id"),
            "allow_remote": state.get("allow_remote", True),
        }
        return [Send("run_agent", {"task": t, **payload}) for t in tasks]

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
    graph.add_node(
        "analyse_root_cause",
        instrumented("root_cause_analysis", "analyse_root_cause", analyse_root_cause),
    )
    graph.add_node("report", instrumented("supervisor", "report", report))

    graph.add_edge(START, "classify")
    graph.add_conditional_edges("classify", after_classify, ["clarify", "resolve_resources"])
    graph.add_edge("clarify", "classify")
    graph.add_edge("resolve_resources", "plan_tasks")
    graph.add_conditional_edges("plan_tasks", fan_out, ["run_agent", "aggregate"])
    graph.add_edge("run_agent", "aggregate")
    graph.add_edge("aggregate", "analyse_root_cause")
    graph.add_edge("analyse_root_cause", "report")
    graph.add_edge("report", END)
    compiled: CompiledStateGraph[Any, Any, Any, Any] = graph.compile(checkpointer=checkpointer)
    return compiled


FINDINGS_EXAMPLE: dict[str, Any] = {
    "summary": "CPU on app-01 is saturated by one python process.",
    "observations": [
        {
            "statement": "python worker.py (PID 4242) uses 97% CPU",
            "kind": "confirmed_fact",
            "evidence_ids": ["E2.2"],
        }
    ],
    "hypotheses": [
        {
            "statement": "worker.py is stuck in a loop",
            "likelihood": "medium",
            "evidence_ids": ["E2.2"],
        }
    ],
}


# ------------------------------------------------------------------- the report


def _flat_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for f in findings:
        for o in f.get("observations") or []:
            out.append({"agent": f["agent"], **o})
        for h in f.get("hypotheses") or []:
            out.append({"agent": f["agent"], "kind": "hypothesis", **h})
    return out


def _conclusion(
    c: dict[str, Any],
    root: dict[str, Any] | None,
    evidence: list[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> str:
    if c.get("request_type") == "change_impact":
        impact = next((f.get("data") for f in findings if f["agent"] == "change_impact"), None)
        if impact and impact.get("impacts"):
            names = ", ".join(i["resource"] for i in impact["impacts"])
            return f"Change risk for {names}: {impact['risk_level']}. See precautions."
        return "The change target could not be resolved in the inventory, so impact is unknown."
    if root and root["status"] == "confirmed":
        return f"Root cause (confirmed, confidence {root['confidence']:.0%}): {root['statement']}"
    if root and root["status"] == "probable":
        return (
            f"Probable root cause (confidence {root['confidence']:.0%}): {root['statement']} "
            "The evidence points to this but does not fully confirm it."
        )
    if evidence:
        return (
            f"{len(evidence)} evidence item(s) were collected but they do not establish a root "
            "cause. See the findings and evidence gaps."
        )
    return (
        "No diagnostic evidence was collected, so no root cause can be stated. "
        "Check that the affected systems are in the inventory and reachable."
    )


def build_report(state: InvestigationState) -> dict[str, Any]:
    """Deterministic RCA report (spec §6.6): states what is known, and what is not."""
    c = state.get("classification", {})
    findings = state.get("findings", [])
    tasks = state.get("tasks", [])
    analysis = state.get("rca") or {}
    by_task = {f["task_id"]: f for f in findings}
    evidence = [e for f in findings for e in f.get("evidence", [])]
    task_rows = [
        {
            **t,
            "status": by_task.get(t["id"], {}).get("status", "not_run"),
            "reason": by_task.get(t["id"], {}).get("reason"),
            "summary": by_task.get(t["id"], {}).get("summary"),
            "notes": by_task.get(t["id"], {}).get("notes", []),
        }
        for t in tasks
    ]
    root = analysis.get("root_cause")
    alternatives = list(analysis.get("alternative_hypotheses") or [])
    # An unverified hypothesis is never reported as the root cause.
    if root and root.get("status") == "unverified":
        alternatives.insert(
            0,
            {"statement": root["statement"], "likelihood": "low", "evidence_ids": []},
        )
        root = None
    sufficient = bool(root)
    timeline = sorted(
        (
            {
                "at": e.get("observed_at"),
                "agent": e.get("agent"),
                "evidence_id": e.get("id"),
                "summary": e.get("summary"),
            }
            for e in evidence
            if e.get("source") == "tool"
        ),
        key=lambda x: x["at"] or "",
    )
    changes = [e for e in evidence if e.get("tool") == "recent_changes"]
    dependency = [
        {
            "resource": e.get("resource"),
            "depends_on": e.get("depends_on", []),
            "dependents": e.get("dependents", 0),
            "evidence_id": e.get("id"),
        }
        for e in evidence
        if e.get("source") == "inventory" and e.get("agent") == "discovery"
    ]
    impact = next((f.get("data") for f in findings if f["agent"] == "change_impact"), None)
    triage = next((f.get("data") for f in findings if f["agent"] == "incident_triage"), None)
    knowledge = next((f.get("data") for f in findings if f["agent"] == "knowledge"), None)
    # Completed tasks are reported as evidence; only the rest remain as next steps.
    next_steps = [
        f"{t['agent'].replace('_', ' ').title()}: {t['objective']}"
        for t in task_rows
        if t["status"] not in ("completed",)
    ]
    next_steps += [f"Close evidence gap: {g}" for g in analysis.get("evidence_gaps") or []]
    if not next_steps:
        next_steps = (
            ["Review the recommended remediation with the resource owner."]
            if sufficient
            else ["Refine the request with a specific system, service or symptom."]
        )
    return {
        "workflow": WORKFLOW_ID,
        # 1. incident summary
        "summary": c.get("summary", ""),
        "incident_summary": analysis.get("incident_summary") or c.get("summary", ""),
        "request_type": c.get("request_type"),
        "urgency": c.get("urgency"),
        "classification_confidence": c.get("confidence"),
        "classification_verified": bool(state.get("resources"))
        and all(r.get("resolved") for r in state.get("resources", [])),
        "affected_entities": c.get("affected_entities", []),
        # 2. affected resources
        "resources": state.get("resources", []),
        "clarifications": state.get("clarifications", []),
        "tasks": task_rows,
        # 3. timeline
        "timeline": timeline,
        # 4. symptoms
        "symptoms": c.get("symptoms", []),
        # 5. diagnostic evidence and findings
        "evidence": evidence,
        "findings": _flat_findings(findings),
        "evidence_sufficient": sufficient,
        # 6. recent changes
        "recent_changes": [
            {"evidence_id": e["id"], "resource": e.get("resource"), "summary": e.get("summary")}
            for e in changes
        ],
        # 7. dependency analysis
        "dependency_analysis": dependency,
        "change_impact": impact,
        # 8-10. root cause, contributing factors, confidence
        "root_cause": root,
        "contributing_factors": analysis.get("contributing_factors") or [],
        "alternative_hypotheses": alternatives,
        "confidence": root["confidence"] if root else None,
        "confidence_label": analysis.get("confidence_label") if root else "none",
        "impact": analysis.get("impact") or "",
        # 11-12. remediation (advisory) and prevention
        "recommended_remediation": analysis.get("recommended_remediation") or [],
        "preventive_recommendations": analysis.get("preventive_recommendations") or [],
        "evidence_gaps": analysis.get("evidence_gaps") or [],
        "triage": triage,
        "similar_incidents": (knowledge or {}).get("similar_incidents", []),
        "similar_investigations": (knowledge or {}).get("similar_investigations", []),
        "conclusion": _conclusion(c, root, evidence, findings),
        "recommended_next_steps": next_steps,
    }
