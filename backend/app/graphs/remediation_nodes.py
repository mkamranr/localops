"""Remediation part of the investigation graph (spec §6.8, §7.6, phase 5).

    analyse_root_cause ──► plan_remediation ──► request_approval (interrupt) ──►
        execute_remediation ──► verify_remediation ──► (next step) … ──► report
                         └── changes requested ──► replan_step ──┘

The plan lives in the checkpointed state as ``remediation``; the database
(approvals, execution jobs) is the source of truth for decisions and runs, so a node
that re-runs after a resume or a crash re-reads it and never acts twice:

* ``request_approval`` creates the approval idempotently (bound to the action hash)
  and pauses with ``interrupt()`` while it is pending. Any resume only wakes it up;
  the decision is read from the approvals table.
* ``execute_remediation`` goes through the execution gateway, which allows one job per
  approval; a re-run returns the recorded job.
* ``verify_remediation`` decides deterministically (``app.agents.verification``).

In ``manual`` mode each step first waits for a user to request its approval (or skip
it); in ``assisted`` mode approvals are requested at once; in ``autonomous`` mode steps
that policy marks auto-approvable run without a pause and the rest wait for a human.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.types import interrupt

from app.agents import remediation as planner
from app.agents import verification
from app.agents.catalogue import get_spec
from app.graphs.runtime import AgentUnavailableError, InvestigationRuntime, NodeCall
from app.llm.gateway import ModelError
from app.remediation.gateway import GatewayRejectedError
from app.tools.registry import TOOLS, change_tools

NodeFn = Callable[[Any, NodeCall], Awaitable[dict[str, Any]]]
Instrument = Callable[[str, str, NodeFn], Callable[[Any], Any]]

MAX_REVISIONS = 2
# Decisions that end a step without running it.
CLOSED = {
    "rejected": "rejected",
    "cancelled": "cancelled",
    "expired": "expired",
    "failed": "failed",
    "executed": "approved",  # executed earlier; the job is looked up again
}


def _evidence(state: Any) -> list[dict[str, Any]]:
    return [e for f in state.get("findings", []) for e in f.get("evidence", [])]


def _parse_resume(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _current(rem: dict[str, Any], *statuses: str) -> dict[str, Any] | None:
    return next((s for s in rem.get("steps", []) if s["status"] in statuses), None)


def _replace(rem: dict[str, Any], step: dict[str, Any], **extra: Any) -> dict[str, Any]:
    steps = [step if s["index"] == step["index"] else s for s in rem.get("steps", [])]
    return {**rem, "steps": steps, **extra}


def _stop_rest(rem: dict[str, Any], reason: str) -> dict[str, Any]:
    steps = [
        {**s, "status": "not_run", "status_reason": reason} if s["status"] == "proposed" else s
        for s in rem.get("steps", [])
    ]
    return {**rem, "steps": steps, "status": "stopped", "reason": reason}


def _cited(evidence: list[dict[str, Any]], ids: list[str]) -> list[dict[str, Any]]:
    wanted = set(ids)
    return [
        {
            "id": e["id"],
            "tool": e.get("tool") or e.get("source"),
            "resource": e.get("resource"),
            "summary": str(e.get("summary") or "")[:500],
            "excerpt": str(e.get("excerpt") or "")[:1500],
        }
        for e in evidence
        if e.get("id") in wanted
    ]


def _short(result: Any) -> dict[str, Any]:
    return {"ok": result.ok, "summary": result.summary, "data": result.data}


def build_remediation_nodes(
    rt: InvestigationRuntime, instrumented: Instrument
) -> dict[str, Callable[[Any], Any]]:
    settings = rt.settings

    async def make_step(
        tool: str, arguments: dict[str, Any], mode: str, index: int, extra: dict[str, Any]
    ) -> dict[str, Any]:
        from app.remediation.workflow import step_from

        prepared = await rt.prepare_remediation_step(tool, arguments, mode)
        return step_from(prepared, index, extra)

    # ------------------------------------------------------------------ planning

    async def plan_remediation(state: Any, call: NodeCall) -> dict[str, Any]:
        mode = state.get("execution_mode") or "assisted"
        ctx = await rt.remediation_context()
        base: dict[str, Any] = {
            "status": "none",
            "reason": None,
            "notes": [],
            "steps": [],
            "mode": mode,
            "requester_id": ctx.get("requester_id"),
            "can_remediate": bool(ctx.get("can_remediate")),
        }
        analysis = state.get("rca") or {}
        resources = state.get("resources", [])
        tools = planner.applicable_tools(change_tools(), resources)
        spec = get_spec("remediation_planner")
        binding = await rt.bind_agent("remediation_planner")
        reason: str | None = None
        if not ctx.get("enabled", True):
            reason = "Remediation is disabled on this installation (REMEDIATION_ENABLED)."
        elif (state.get("classification") or {}).get("request_type") == "change_impact":
            reason = "Change impact request: no remediation is planned."
        elif why := planner.can_plan(analysis):
            reason = why
        elif not tools:
            reason = "No registered remediation tool applies to the affected resources."
        elif spec is None or not spec.implemented or not binding.enabled:
            reason = "The Remediation Planner agent is disabled."
        if reason:
            rem = {**base, "status": "skipped", "reason": reason}
            await rt.emit("remediation.skipped", "remediation_planner", {"reason": reason})
            return {"remediation": rem}

        evidence = _evidence(state)
        try:
            plan = await rt.structured(
                call,
                binding,
                planner.plan_messages(
                    state["request"],
                    analysis,
                    evidence,
                    resources,
                    tools,
                    max_steps=settings.remediation_max_steps,
                ),
                planner.RemediationPlan,
                example=planner.PLAN_EXAMPLE,
            )
        except (ModelError, AgentUnavailableError) as exc:
            rem = {**base, "status": "skipped", "reason": f"Planning failed: {str(exc)[:200]}"}
            await rt.emit("remediation.skipped", "remediation_planner", {"reason": rem["reason"]})
            return {"remediation": rem}

        known = {e["id"] for e in evidence if e.get("id")}
        allowed = {t.name for t in tools}
        notes: list[str] = [plan.notes] if plan.notes else []
        steps: list[dict[str, Any]] = []
        seen: set[str] = set()
        for proposed in plan.steps[: settings.remediation_max_steps]:
            if proposed.tool not in allowed:
                notes.append(f"{proposed.tool}: dropped, not a registered remediation tool here")
                continue
            if note := planner.check_citations(proposed, known):
                notes.append(note)
                continue
            try:
                step = await make_step(
                    proposed.tool,
                    proposed.arguments,
                    mode,
                    len(steps),
                    {
                        "rationale": proposed.rationale[:1000],
                        "expected_impact": proposed.expected_impact[:500],
                        "evidence_ids": proposed.evidence_ids,
                        "evidence": _cited(evidence, proposed.evidence_ids),
                    },
                )
            except GatewayRejectedError as exc:
                notes.append(f"{proposed.tool}: dropped, {exc}")
                continue
            key = json.dumps([step["tool"], step["arguments"]], sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            steps.append(step)

        if not steps:
            status, reason = "none", "No applicable remediation was proposed."
        elif not base["can_remediate"]:
            status = "advisory"
            reason = (
                "The requester lacks tool.execute.remediation, so the plan is advisory and "
                "nothing will run."
            )
            steps = [
                {**s, "status": "advisory", "status_reason": reason}
                if s["status"] == "proposed"
                else s
                for s in steps
            ]
        else:
            status, reason = "planned", None
        rem = {**base, "status": status, "reason": reason, "notes": notes, "steps": steps}
        await rt.emit(
            "remediation.planned",
            "remediation_planner",
            {"status": status, "reason": reason, "notes": notes, "steps": steps},
        )
        if steps:
            await rt.incident_step(
                "remediation_planned",
                f"Remediation plan with {len(steps)} step(s): "
                + "; ".join(s["description"] for s in steps),
                details={"status": status},
            )
        return {"remediation": rem}

    async def replan_step(state: Any, call: NodeCall) -> dict[str, Any]:
        rem = dict(state["remediation"])
        step = _current(rem, "changes_requested")
        if step is None:  # pragma: no cover - routing only sends us here with one
            return {}
        feedback = step.get("feedback") or ""
        if step.get("revisions", 0) >= MAX_REVISIONS:
            dropped = {**step, "status": "dropped", "status_reason": "Too many revisions"}
            return {"remediation": _replace(rem, dropped)}
        binding = await rt.bind_agent("remediation_planner")
        evidence = _evidence(state)
        tools = planner.applicable_tools(change_tools(), state.get("resources", []))
        previous = {k: step.get(k) for k in ("tool", "arguments", "rationale", "expected_impact")}
        try:
            plan = await rt.structured(
                call,
                binding,
                planner.plan_messages(
                    state["request"],
                    state.get("rca") or {},
                    evidence,
                    state.get("resources", []),
                    tools,
                    max_steps=1,
                    feedback=feedback,
                    previous=previous,
                ),
                planner.RemediationPlan,
                example=planner.PLAN_EXAMPLE,
            )
        except (ModelError, AgentUnavailableError) as exc:
            dropped = {**step, "status": "dropped", "status_reason": f"Re-planning failed: {exc}"}
            return {"remediation": _replace(rem, dropped)}
        reason: str | None = None
        revised: dict[str, Any] | None = None
        if not plan.steps:
            reason = "The planner dropped the step after the requested changes"
        else:
            p = plan.steps[0]
            if p.tool not in {t.name for t in tools}:
                reason = f"Revision used an unavailable tool ({p.tool})"
            elif note := planner.check_citations(p, {e["id"] for e in evidence if e.get("id")}):
                reason = note
            else:
                try:
                    revised = await make_step(
                        p.tool,
                        p.arguments,
                        rem.get("mode", "assisted"),
                        step["index"],
                        {
                            "rationale": p.rationale[:1000],
                            "expected_impact": p.expected_impact[:500],
                            "evidence_ids": p.evidence_ids,
                            "evidence": _cited(evidence, p.evidence_ids),
                            "revisions": step.get("revisions", 0) + 1,
                            "feedback": feedback,
                        },
                    )
                except GatewayRejectedError as exc:
                    reason = f"Revision rejected: {exc}"
                if revised and (revised["tool"], revised["arguments"]) == (
                    step["tool"],
                    step["arguments"],
                ):
                    reason, revised = "The revision did not change the action", None
        if revised is None:
            new = {**step, "status": "dropped", "status_reason": reason}
        else:
            new = revised
        await rt.emit(
            "remediation.revised",
            "remediation_planner",
            {"step_index": step["index"], "step": new, "feedback": feedback},
        )
        return {"remediation": _replace(rem, new)}

    # ------------------------------------------------------------------ approval

    async def request_approval(state: Any) -> dict[str, Any]:
        await rt.check_cancelled()
        rem = dict(state["remediation"])
        step = _current(rem, "proposed")
        if step is None:  # pragma: no cover - routing only sends us here with one
            return {}
        requester = rem.get("requester_id")
        if rem.get("mode") == "manual":
            # Wait until a user asks for this step's approval, or skips it.
            while True:
                answer = _parse_resume(
                    interrupt(
                        {
                            "kind": "remediation_step",
                            "step_index": step["index"],
                            "description": step["description"],
                            "risk_level": step["risk_level"],
                        }
                    )
                )
                if (
                    answer.get("kind") == "remediation_step"
                    and answer.get("step_index") == step["index"]
                ):
                    break
            if answer.get("action") == "skip":
                skipped = {
                    **step,
                    "status": "skipped",
                    "status_reason": "Skipped by the requester",
                }
                await rt.emit(
                    "remediation.step_skipped",
                    "supervisor",
                    {"step_index": step["index"], "reason": skipped["status_reason"]},
                )
                return {"remediation": _replace(rem, skipped)}
            requester = answer.get("user_id") or requester
        if not requester:
            blocked = {**step, "status": "blocked", "status_reason": "No requester"}
            return {"remediation": _replace(rem, blocked)}

        try:
            approval = await rt.ensure_approval(step, str(requester))
        except GatewayRejectedError as exc:
            blocked = {**step, "status": "blocked", "status_reason": str(exc)}
            await rt.emit(
                "remediation.step_skipped",
                "supervisor",
                {"step_index": step["index"], "reason": str(exc)},
            )
            return {"remediation": _replace(rem, blocked)}
        if approval.get("created"):
            await rt.emit(
                "approval.auto_approved" if approval["auto_approved"] else "approval.requested",
                "supervisor",
                {
                    "step_index": step["index"],
                    "approval_id": approval["approval_id"],
                    "description": step["description"],
                    "risk_level": approval["risk_level"],
                    "expires_at": approval["expires_at"],
                },
            )

        info = await rt.approval_info(approval["approval_id"])
        while info["status"] == "pending":
            interrupt(
                {
                    "kind": "approval",
                    "approval_id": info["approval_id"],
                    "step_index": step["index"],
                    "description": step["description"],
                    "risk_level": info["risk_level"],
                    "expires_at": info["expires_at"],
                }
            )
            info = await rt.approval_info(approval["approval_id"])

        status = info["status"]
        decided = {
            **step,
            "approval_id": info["approval_id"],
            "approval": {k: info.get(k) for k in ("status", "auto_approved", "comment")},
        }
        if status in ("approved", "executed"):
            decided["status"] = "approved"
        elif status == "changes_requested":
            decided["status"] = "changes_requested"
            decided["feedback"] = (info.get("comment") or "")[:1000]
        else:
            decided["status"] = CLOSED.get(status, "rejected")
            decided["status_reason"] = info.get("comment") or f"Approval {status}"
            await rt.emit(
                "remediation.step_skipped",
                "supervisor",
                {"step_index": step["index"], "reason": decided["status_reason"]},
            )
        return {"remediation": _replace(rem, decided)}

    # ------------------------------------------------------------------ execution

    async def run_checks(step: dict[str, Any], tag: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        checks = verification.checks_for(
            step["tool"], tuple(step.get("verify_with") or ()), step["arguments"]
        )
        for n, check in enumerate(checks, start=1):
            spec = TOOLS.get(check.tool)
            if spec is None:
                continue
            result = await rt.run_tool(
                check.tool,
                check.arguments,
                f"R{step['index'] + 1}.{tag}{n}",
                agent_type="verification",
                allowed=[spec],
                allow_remote=True,
            )
            out[check.tool] = _short(result)
        return out

    async def execute_remediation(state: Any) -> dict[str, Any]:
        await rt.check_cancelled()
        rem = dict(state["remediation"])
        step = _current(rem, "approved")
        if step is None:  # pragma: no cover
            return {}
        before = step.get("before")
        if before is None:
            before = await run_checks(step, "b")
        await rt.incident_step(
            "remediation_started",
            f"Executing: {step['description']}",
            status="remediating",
            details={"approval_id": step["approval_id"]},
        )
        await rt.emit(
            "execution.started",
            "supervisor",
            {
                "step_index": step["index"],
                "approval_id": step["approval_id"],
                "tool": step["tool"],
                "description": step["description"],
            },
        )
        result = await rt.execute_approval(step["approval_id"], before)
        await rt.emit(
            "execution.completed",
            "supervisor",
            {
                "step_index": step["index"],
                "job_id": result.get("job_id"),
                "status": result.get("status"),
                "summary": result.get("summary"),
                "duration_ms": result.get("duration_ms"),
            },
        )
        done = {
            **step,
            "before": before,
            "job_id": result.get("job_id"),
            "execution": {
                k: result.get(k) for k in ("status", "summary", "error_code", "duration_ms", "data")
            },
            "status": "executed" if result.get("status") == "succeeded" else "failed",
        }
        if done["status"] == "failed":
            done["status_reason"] = result.get("summary")
            reason = f"Step {step['index'] + 1} did not complete: {result.get('summary')}"
            await rt.incident_step(
                "remediation_failed",
                reason,
                status="investigating",
                details={"job_id": result.get("job_id")},
            )
            return {"remediation": _stop_rest(_replace(rem, done), reason)}
        return {"remediation": _replace(rem, done)}

    async def verify_remediation(state: Any, call: NodeCall) -> dict[str, Any]:
        rem = dict(state["remediation"])
        step = _current(rem, "executed")
        if step is None:  # pragma: no cover
            return {}
        reboot = step["tool"].endswith("_reboot")
        attempts = 4 if reboot else 1
        delay = settings.verification_delay_seconds * (6 if reboot else 1)
        after: dict[str, dict[str, Any]] = {}
        verdict = verification.Verdict("failed", "Not verified", [])
        for _ in range(attempts):
            if delay > 0:
                await asyncio.sleep(delay)
            await rt.check_cancelled()
            after = await run_checks(step, "a")
            verdict = verification.judge(
                step["tool"],
                step["arguments"],
                step.get("execution") or {},
                after,
                step.get("before"),
            )
            if verdict.status == "passed":
                break
        data = verdict.as_dict()
        await rt.record_verification(step.get("job_id"), after, data)
        await rt.emit(
            "verification.completed",
            "verification",
            {"step_index": step["index"], "job_id": step.get("job_id"), **data},
        )
        checked = {
            **step,
            "after": after,
            "verification": data,
            "status": "verified" if verdict.status == "passed" else "unverified",
        }
        rem = _replace(rem, checked)
        if verdict.status == "passed":
            last = _current(rem, "proposed") is None
            await rt.incident_step(
                "remediation_verified",
                data["summary"],
                status="monitoring" if last else None,
                details={"job_id": step.get("job_id")},
            )
            return {"remediation": rem}
        reason = f"Step {step['index'] + 1} was not verified: {data['summary']}"
        await rt.incident_step(
            "verification_failed",
            f"{reason}. Escalate to the resource owner.",
            status="investigating",
            details={"job_id": step.get("job_id")},
        )
        return {"remediation": _stop_rest(rem, reason)}

    return {
        "plan_remediation": instrumented(
            "remediation_planner", "plan_remediation", plan_remediation
        ),
        "replan_step": instrumented("remediation_planner", "replan_step", replan_step),
        "request_approval": request_approval,
        "execute_remediation": execute_remediation,
        "verify_remediation": instrumented(
            "verification", "verify_remediation", verify_remediation
        ),
    }


# ------------------------------------------------------------------ routing


def next_step(state: Any) -> str:
    rem = state.get("remediation") or {}
    if rem.get("status") == "planned" and _current(rem, "proposed"):
        return "request_approval"
    return "report"


def after_approval(state: Any) -> str:
    rem = state.get("remediation") or {}
    if _current(rem, "approved"):
        return "execute_remediation"
    if _current(rem, "changes_requested"):
        return "replan_step"
    return next_step(state)


def after_execution(state: Any) -> str:
    rem = state.get("remediation") or {}
    if _current(rem, "executed"):
        return "verify_remediation"
    return next_step(state)


def finalise(rem: dict[str, Any] | None) -> dict[str, Any] | None:
    """The remediation section of the report: final status and slimmed-down steps."""
    if not rem:
        return None
    steps = []
    for s in rem.get("steps", []):
        steps.append(
            {
                k: s.get(k)
                for k in (
                    "index",
                    "tool",
                    "arguments",
                    "resource_id",
                    "resource_name",
                    "resource_type",
                    "description",
                    "risk_level",
                    "policy",
                    "status",
                    "status_reason",
                    "rationale",
                    "expected_impact",
                    "evidence_ids",
                    "approval_id",
                    "approval",
                    "job_id",
                    "execution",
                    "verification",
                    "revisions",
                )
            }
        )
    status = rem.get("status")
    if status == "planned":
        if any(s["status"] == "unverified" for s in steps):
            status = "unverified"
        elif any(s["status"] == "verified" for s in steps):
            status = "verified"
        else:
            status = "not_executed"
    return {
        "status": status,
        "reason": rem.get("reason"),
        "mode": rem.get("mode"),
        "notes": rem.get("notes", []),
        "steps": steps,
    }
