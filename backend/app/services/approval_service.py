"""Approval requests for remediation actions (spec §7.6, §10.6, §12.10).

An approval binds one exact action (tool + canonical arguments + resource +
investigation, as ``action_hash``) to a human decision. Deciding resumes the paused
investigation graph, which then asks the execution gateway to run the action; the
gateway re-validates everything at that moment.

Four-eyes: the person who started the investigation may not approve its actions
unless the matching policy allows self-approval (never for critical actions).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.database.session import tenant_session
from app.graphs import events
from app.graphs.dispatch import InvestigationDispatcher
from app.models import Approval, ExecutionJob, Incident, Investigation, User, incident_key
from app.remediation.gateway import PreparedAction
from app.schemas.common import PageParams
from app.schemas.remediation import ApprovalDetail, ApprovalOut, ApprovalSummary, ExecutionOut
from app.services import audit_service as audit
from app.services import incident_service
from app.services.context import ClientInfo, Principal, RequestContext

Decision = Literal["approve", "reject", "request_changes", "cancel"]
STATUS_FOR: dict[str, str] = {
    "approve": "approved",
    "reject": "rejected",
    "request_changes": "changes_requested",
    "cancel": "cancelled",
}
VERB = {
    "approve": "approved",
    "reject": "rejected",
    "request_changes": "requested changes to",
    "cancel": "cancelled",
}


def resume_payload(approval: Approval) -> str:
    return json.dumps(
        {"kind": "approval", "approval_id": str(approval.id), "status": approval.status}
    )


# ------------------------------------------------------------------ creation (graph)


async def create_for_step(
    s: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    investigation: Investigation,
    step_index: int,
    prepared: PreparedAction,
    rationale: str,
    expected_impact: str,
    evidence_ids: list[str],
    requested_by: uuid.UUID,
    evidence: list[dict[str, Any]] | None = None,
) -> Approval:
    """Create the approval for a plan step. When policy lets the step run unattended
    (autonomous mode), the approval is recorded as auto-approved."""
    decision = prepared.decision
    now = datetime.now(UTC)
    # Supersede an open request for the same step (e.g. after "request changes").
    open_rows = await s.scalars(
        select(Approval).where(
            Approval.investigation_id == investigation.id,
            Approval.step_index == step_index,
            Approval.status == "pending",
        )
    )
    for row in open_rows:
        row.status = "cancelled"
        row.decided_at = now
        row.decision_comment = "Superseded by a revised request"
    await s.flush()
    auto = not decision.requires_approval
    approval = Approval(
        tenant_id=tenant_id,
        investigation_id=investigation.id,
        incident_id=investigation.incident_id,
        step_index=step_index,
        requested_by=requested_by,
        action=prepared.spec.name,
        action_parameters=prepared.arguments,
        action_hash=prepared.hash_for(investigation.id),
        description=prepared.description,
        resource_id=prepared.resource.id,
        resource_name=prepared.resource.name,
        risk_level=decision.risk_level,
        rationale=rationale[:4000],
        expected_impact=expected_impact[:4000],
        evidence_ids=list(evidence_ids)[:20],
        evidence=list(evidence or [])[:20],
        policy=decision.as_dict(),
        auto_approved=auto,
        status="approved" if auto else "pending",
        decided_at=now if auto else None,
        decision_comment=(
            f"Auto-approved by policy ({', '.join(decision.matched_rules) or 'default'})"
            if auto
            else None
        ),
        expires_at=now + timedelta(minutes=decision.approval_ttl_minutes),
    )
    s.add(approval)
    await s.flush()
    await audit.record(
        s,
        tenant_id=tenant_id,
        action="approval.auto_approve" if auto else "approval.request",
        user_id=requested_by,
        entity_type="approval",
        entity_id=approval.id,
        details={
            "tool": approval.action,
            "arguments": approval.action_parameters,
            "resource": approval.resource_name,
            "risk_level": approval.risk_level,
            "investigation_id": str(investigation.id),
            "matched_rules": decision.matched_rules,
        },
    )
    return approval


# ------------------------------------------------------------------ reads


async def _usernames(s: AsyncSession, raw: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    ids = {i for i in raw if i is not None}
    if not ids:
        return {}
    rows = await s.execute(select(User.id, User.username).where(User.id.in_(ids)))
    return {r.id: r.username for r in rows}


async def _expire_if_due(s: AsyncSession, approval: Approval) -> bool:
    if approval.status == "pending" and approval.expires_at < datetime.now(UTC):
        approval.status = "expired"
        approval.decided_at = datetime.now(UTC)
        await s.flush()
        return True
    return False


async def _out(s: AsyncSession, rows: list[Approval]) -> list[ApprovalOut]:
    wanted: set[uuid.UUID | None] = {r.requested_by for r in rows}
    wanted |= {r.approved_by for r in rows}
    names = await _usernames(s, wanted)
    out = []
    for r in rows:
        item = ApprovalOut.model_validate(r)
        item.requested_by_username = names.get(r.requested_by)
        item.approved_by_username = names.get(r.approved_by) if r.approved_by else None
        out.append(item)
    return out


def decide_block_reason(p: Principal, approval: Approval) -> str | None:
    """Why ``p`` may not approve/reject/request changes; ``None`` if they may."""
    if approval.status != "pending":
        return f"The request is {approval.status}"
    if not p.has("approval.approve"):
        return "You need the approval.approve permission"
    self_ok = bool(approval.policy.get("allow_self_approval")) and approval.risk_level != "critical"
    if p.user_id == approval.requested_by and not self_ok:
        return "You requested this action; another person must approve it"
    return None


async def list_approvals(
    ctx: RequestContext,
    page: PageParams,
    *,
    status: str | None = None,
    risk: str | None = None,
    mine: bool = False,
    investigation_id: uuid.UUID | None = None,
    incident_id: uuid.UUID | None = None,
) -> tuple[list[ApprovalOut], int]:
    s = ctx.session
    q = select(Approval)
    if incident_id:
        q = q.where(Approval.incident_id == incident_id)
    if status:
        q = q.where(Approval.status == status)
    if risk:
        q = q.where(Approval.risk_level == risk)
    if mine:
        q = q.where(Approval.requested_by == ctx.principal.user_id)
    if investigation_id:
        q = q.where(Approval.investigation_id == investigation_id)
    total = await s.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = list(
        await s.scalars(
            q.order_by(Approval.created_at.desc()).offset(page.offset).limit(page.page_size)
        )
    )
    for r in rows:
        await _expire_if_due(s, r)
    return await _out(s, rows), total


async def get(ctx: RequestContext, approval_id: uuid.UUID) -> ApprovalDetail:
    s = ctx.session
    approval = await s.get(Approval, approval_id)
    if approval is None:
        raise NotFoundError("Approval not found")
    await _expire_if_due(s, approval)
    base = (await _out(s, [approval]))[0]
    detail = ApprovalDetail(**base.model_dump())
    inv = await s.get(Investigation, approval.investigation_id)
    if inv is not None:
        detail.investigation_title = inv.title
        wanted = set(approval.evidence_ids)
        detail.evidence = list(approval.evidence) or [
            e
            for e in (inv.result or {}).get("evidence", [])
            if isinstance(e, dict) and e.get("id") in wanted
        ]
    if approval.incident_id:
        incident = await s.get(Incident, approval.incident_id)
        detail.incident_key = incident_key(incident.number) if incident else None
    reason = decide_block_reason(ctx.principal, approval)
    detail.can_decide = reason is None
    detail.cannot_decide_reason = reason
    job = await s.scalar(select(ExecutionJob).where(ExecutionJob.approval_id == approval.id))
    detail.execution = ExecutionOut.model_validate(job) if job else None
    return detail


async def summary(ctx: RequestContext) -> ApprovalSummary:
    s = ctx.session
    now = datetime.now(UTC)
    pending = Approval.status == "pending"
    live = Approval.expires_at > now
    rows = await s.execute(
        select(Approval.risk_level, func.count()).where(pending, live).group_by(Approval.risk_level)
    )
    by_risk = {r[0]: int(r[1]) for r in rows}
    p = ctx.principal
    candidates = list(await s.scalars(select(Approval).where(pending, live).limit(500)))
    for_me = sum(1 for a in candidates if decide_block_reason(p, a) is None)
    soon = await s.scalar(
        select(func.count()).where(pending, live, Approval.expires_at < now + timedelta(minutes=15))
    )
    return ApprovalSummary(
        pending=sum(by_risk.values()),
        pending_for_me=for_me,
        by_risk=by_risk,
        expiring_soon=int(soon or 0),
    )


# ------------------------------------------------------------------ decisions


async def decide(
    principal: Principal,
    client: ClientInfo,
    approval_id: uuid.UUID,
    decision: Decision,
    comment: str | None,
    dispatcher: InvestigationDispatcher,
    redis: Redis | None = None,
) -> ApprovalOut:
    async with tenant_session(principal.tenant_id) as s:
        approval = await s.get(Approval, approval_id, with_for_update=True)
        if approval is None:
            raise NotFoundError("Approval not found")
        if await _expire_if_due(s, approval):
            await _after_decision(s, principal, client, approval, "expire", None)
            out = (await _out(s, [approval]))[0]
            expired = True
        else:
            expired = False
            if decision == "cancel":
                if approval.status != "pending":
                    raise ConflictError(f"The request is {approval.status}")
                if principal.user_id != approval.requested_by and not principal.has(
                    "approval.approve"
                ):
                    raise PermissionDeniedError(
                        "Only the requester or an approver may cancel this request"
                    )
            else:
                reason = decide_block_reason(principal, approval)
                if reason and approval.status != "pending":
                    raise ConflictError(reason, details={"status": approval.status})
                if reason:
                    raise PermissionDeniedError(reason, code="self_approval")
            approval.status = STATUS_FOR[decision]
            approval.approved_by = principal.user_id
            approval.decided_at = datetime.now(UTC)
            approval.decision_comment = comment
            await _after_decision(s, principal, client, approval, decision, comment)
            out = (await _out(s, [approval]))[0]
        investigation_id = approval.investigation_id
        payload = resume_payload(approval)
    await events.notify(redis, principal.tenant_id, investigation_id)
    await dispatcher.enqueue(principal.tenant_id, investigation_id, resume=payload)
    if expired:
        raise ConflictError("The approval request expired before it was decided")
    return out


async def _after_decision(
    s: AsyncSession,
    principal: Principal | None,
    client: ClientInfo | None,
    approval: Approval,
    decision: str,
    comment: str | None,
) -> None:
    """Shared bookkeeping: investigation status/event, incident timeline, audit."""
    who = principal.username if principal else "system"
    inv = await s.get(Investigation, approval.investigation_id, with_for_update=True)
    if inv is not None and inv.status == "awaiting_approval":
        inv.status = "queued"  # the runner resumes it
    await events.append_event(
        s,
        tenant_id=approval.tenant_id,
        investigation_id=approval.investigation_id,
        event_type="approval.decided",
        data={
            "approval_id": str(approval.id),
            "step_index": approval.step_index,
            "status": approval.status,
            "decided_by": who,
            "comment": comment,
            "action": approval.action,
        },
    )
    verb = "expired" if decision == "expire" else VERB.get(decision, decision)
    await incident_service.workflow_update(
        s,
        approval.incident_id,
        "approval_decided",
        f"{who} {verb} '{approval.description[:200]}'"
        if decision != "expire"
        else f"Approval for '{approval.description[:200]}' expired",
        actor=who,
        details={"approval_id": str(approval.id), "status": approval.status, "comment": comment},
    )
    await audit.record(
        s,
        tenant_id=approval.tenant_id,
        actor=principal,
        username=None if principal else "system",
        action=f"approval.{decision}",
        entity_type="approval",
        entity_id=approval.id,
        details={
            "tool": approval.action,
            "arguments": approval.action_parameters,
            "resource": approval.resource_name,
            "risk_level": approval.risk_level,
            "investigation_id": str(approval.investigation_id),
            "comment": comment,
        },
        client=client,
    )


async def expire_overdue(
    rows: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]],
    dispatcher: InvestigationDispatcher,
    redis: Redis | None = None,
) -> int:
    """Expire overdue requests (ids from ``worker_expired_approvals()``) and resume
    their investigations so the step is recorded as not executed."""
    done = 0
    for approval_id, tenant_id, investigation_id in rows:
        async with tenant_session(tenant_id) as s:
            approval = await s.get(Approval, approval_id, with_for_update=True)
            if approval is None or not await _expire_if_due(s, approval):
                continue
            await _after_decision(s, None, None, approval, "expire", None)
            payload = resume_payload(approval)
        await events.notify(redis, tenant_id, investigation_id)
        await dispatcher.enqueue(tenant_id, investigation_id, resume=payload)
        done += 1
    return done


async def cancel_for_investigation(s: AsyncSession, investigation_id: uuid.UUID) -> int:
    rows = list(
        await s.scalars(
            select(Approval).where(
                Approval.investigation_id == investigation_id, Approval.status == "pending"
            )
        )
    )
    for r in rows:
        r.status = "cancelled"
        r.decided_at = datetime.now(UTC)
        r.decision_comment = "Investigation cancelled"
    return len(rows)


# ------------------------------------------------------------------ executions


async def list_executions(
    ctx: RequestContext,
    page: PageParams,
    *,
    investigation_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    status: str | None = None,
    incident_id: uuid.UUID | None = None,
) -> tuple[list[ExecutionOut], int]:
    s = ctx.session
    q = select(ExecutionJob)
    if incident_id:
        q = q.where(
            ExecutionJob.investigation_id.in_(
                select(Investigation.id).where(Investigation.incident_id == incident_id)
            )
        )
    if investigation_id:
        q = q.where(ExecutionJob.investigation_id == investigation_id)
    if resource_id:
        q = q.where(ExecutionJob.resource_id == resource_id)
    if status:
        q = q.where(ExecutionJob.status == status)
    total = await s.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = await s.scalars(
        q.order_by(ExecutionJob.started_at.desc()).offset(page.offset).limit(page.page_size)
    )
    return [ExecutionOut.model_validate(r) for r in rows], total


async def get_execution(ctx: RequestContext, job_id: uuid.UUID) -> ExecutionOut:
    job = await ctx.session.get(ExecutionJob, job_id)
    if job is None:
        raise NotFoundError("Execution not found")
    return ExecutionOut.model_validate(job)


def evidence_of(report: dict[str, Any], ids: list[str]) -> list[dict[str, Any]]:
    wanted = set(ids)
    return [e for e in report.get("evidence", []) if isinstance(e, dict) and e.get("id") in wanted]
