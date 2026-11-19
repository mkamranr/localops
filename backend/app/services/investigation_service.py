"""Investigations: create, list, inspect, answer clarifications, cancel.

Operations that hand work to the executor commit their own short transaction first,
so the worker can never observe an investigation that isn't committed yet.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import func, or_, select

from app.core.errors import AppError, ConflictError, NotFoundError
from app.database.session import tenant_session
from app.graphs import events
from app.graphs.dispatch import InvestigationDispatcher
from app.graphs.investigation import WORKFLOW_ID
from app.models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    Incident,
    Investigation,
    InvestigationEvent,
    Resource,
    User,
)
from app.schemas.ai import (
    InvestigationCreate,
    InvestigationDetail,
    InvestigationEventOut,
    InvestigationOut,
)
from app.schemas.common import PageParams
from app.services import approval_service, incident_service
from app.services import audit_service as audit
from app.services.context import ClientInfo, Principal, RequestContext


class ModeUnavailableError(AppError):
    status_code = 422
    code = "mode_unavailable"


def _title(request: str) -> str:
    first = " ".join(request.split())
    return first if len(first) <= 120 else first[:119] + "…"


async def create(
    principal: Principal,
    client: ClientInfo,
    data: InvestigationCreate,
    dispatcher: InvestigationDispatcher,
) -> Investigation:
    async with tenant_session(principal.tenant_id) as s:
        resource_ids = list(dict.fromkeys(data.resource_ids))
        if resource_ids:
            found = set(await s.scalars(select(Resource.id).where(Resource.id.in_(resource_ids))))
            missing = [str(r) for r in resource_ids if r not in found]
            if missing:
                raise NotFoundError("Resource not found", details={"resource_ids": missing})
        if data.incident_id is not None and await s.get(Incident, data.incident_id) is None:
            raise NotFoundError("Incident not found")
        inv = Investigation(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            workflow_id=WORKFLOW_ID,
            title=(data.title or "").strip() or _title(data.request),
            request=data.request,
            status="queued",
            execution_mode=data.execution_mode,
            incident_id=data.incident_id,
            investigation_type=data.investigation_type,
            resource_ids=[str(r) for r in resource_ids],
            result={},
        )
        s.add(inv)
        await s.flush()
        if data.incident_id is not None:
            await incident_service.link_investigation(s, data.incident_id, inv, principal.username)
        await events.append_event(
            s,
            tenant_id=principal.tenant_id,
            investigation_id=inv.id,
            event_type="investigation.queued",
            data={"execution_mode": inv.execution_mode, "requested_by": principal.username},
        )
        await audit.record(
            s,
            tenant_id=principal.tenant_id,
            actor=principal,
            action="investigation.create",
            entity_type="investigation",
            entity_id=inv.id,
            details={"title": inv.title, "mode": inv.execution_mode},
            client=client,
        )
    await dispatcher.enqueue(principal.tenant_id, inv.id)
    return inv


async def list_investigations(
    ctx: RequestContext,
    page: PageParams,
    *,
    status: str | None = None,
    q: str | None = None,
    active: bool | None = None,
) -> tuple[list[Investigation], int]:
    query = select(Investigation)
    if status:
        query = query.where(Investigation.status == status)
    if active is True:
        query = query.where(Investigation.status.in_(ACTIVE_STATUSES))
    if q:
        like = f"%{q.strip()}%"
        query = query.where(or_(Investigation.title.ilike(like), Investigation.request.ilike(like)))
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await ctx.session.scalars(
        query.order_by(Investigation.started_at.desc()).offset(page.offset).limit(page.page_size)
    )
    return list(rows), total


async def get(ctx: RequestContext, investigation_id: uuid.UUID) -> InvestigationDetail:
    inv = await ctx.session.get(Investigation, investigation_id)
    if inv is None:
        raise NotFoundError("Investigation not found")
    username = await ctx.session.scalar(select(User.username).where(User.id == inv.user_id))
    detail = InvestigationDetail.model_validate(inv)
    detail.username = username
    return detail


async def exists(principal: Principal, investigation_id: uuid.UUID) -> None:
    async with tenant_session(principal.tenant_id) as s:
        if await s.get(Investigation, investigation_id) is None:
            raise NotFoundError("Investigation not found")


async def list_events(
    ctx: RequestContext, investigation_id: uuid.UUID, after: int = 0
) -> list[InvestigationEventOut]:
    if await ctx.session.get(Investigation, investigation_id) is None:
        raise NotFoundError("Investigation not found")
    rows = await events.list_events(ctx.session, investigation_id, after=after, limit=1000)
    return [InvestigationEventOut.model_validate(r) for r in rows]


async def resume(
    principal: Principal,
    client: ClientInfo,
    investigation_id: uuid.UUID,
    answer: str,
    dispatcher: InvestigationDispatcher,
    redis: Redis | None = None,
) -> Investigation:
    answer = answer.strip()
    if not answer:
        raise AppError("Answer must not be empty", code="validation_error", status_code=422)
    async with tenant_session(principal.tenant_id) as s:
        inv = await s.get(Investigation, investigation_id, with_for_update=True)
        if inv is None:
            raise NotFoundError("Investigation not found")
        if inv.status != "awaiting_input":
            raise ConflictError(
                "This investigation is not waiting for input", details={"status": inv.status}
            )
        question = inv.pending_question
        inv.status = "queued"
        inv.pending_question = None
        await events.append_event(
            s,
            tenant_id=principal.tenant_id,
            investigation_id=inv.id,
            event_type="clarification.answered",
            data={"question": question, "answer": answer, "answered_by": principal.username},
        )
        await audit.record(
            s,
            tenant_id=principal.tenant_id,
            actor=principal,
            action="investigation.resume",
            entity_type="investigation",
            entity_id=inv.id,
            client=client,
        )
    await events.notify(redis, principal.tenant_id, investigation_id)
    await dispatcher.enqueue(principal.tenant_id, investigation_id, resume=answer)
    return inv


async def remediation_step(
    principal: Principal,
    client: ClientInfo,
    investigation_id: uuid.UUID,
    step_index: int,
    action: str,
    dispatcher: InvestigationDispatcher,
    redis: Redis | None = None,
) -> Investigation:
    """Manual mode: ask for approval of a proposed step (``request``) or ``skip`` it."""
    if action == "request":
        principal.require("tool.execute.remediation")
    async with tenant_session(principal.tenant_id) as s:
        inv = await s.get(Investigation, investigation_id, with_for_update=True)
        if inv is None:
            raise NotFoundError("Investigation not found")
        waiting = await s.scalar(
            select(InvestigationEvent)
            .where(InvestigationEvent.investigation_id == inv.id)
            .order_by(InvestigationEvent.seq.desc())
            .limit(1)
        )
        pending = (
            waiting.event_data
            if waiting is not None and waiting.event_type == "remediation.awaiting_request"
            else None
        )
        if inv.status != "awaiting_approval" or pending is None:
            raise ConflictError(
                "This investigation is not waiting for a remediation request",
                details={"status": inv.status},
            )
        if pending.get("step_index") != step_index:
            raise ConflictError(
                "That step is not the one waiting for a decision",
                details={"step_index": pending.get("step_index")},
            )
        inv.status = "queued"
        inv.pending_question = None
        await events.append_event(
            s,
            tenant_id=principal.tenant_id,
            investigation_id=inv.id,
            event_type="remediation.step_requested"
            if action == "request"
            else "remediation.step_skipped",
            data={"step_index": step_index, "by": principal.username},
        )
        await audit.record(
            s,
            tenant_id=principal.tenant_id,
            actor=principal,
            action=f"remediation.{action}",
            entity_type="investigation",
            entity_id=inv.id,
            client=client,
            details={"step_index": step_index},
        )
    payload = json.dumps(
        {
            "kind": "remediation_step",
            "step_index": step_index,
            "action": action,
            "user_id": str(principal.user_id),
        }
    )
    await events.notify(redis, principal.tenant_id, investigation_id)
    await dispatcher.enqueue(principal.tenant_id, investigation_id, resume=payload)
    return inv


async def cancel(
    principal: Principal,
    client: ClientInfo,
    investigation_id: uuid.UUID,
    redis: Redis | None = None,
) -> InvestigationOut:
    async with tenant_session(principal.tenant_id) as s:
        inv = await s.get(Investigation, investigation_id, with_for_update=True)
        if inv is None:
            raise NotFoundError("Investigation not found")
        if inv.status in TERMINAL_STATUSES:
            raise ConflictError(
                "Investigation has already finished", details={"status": inv.status}
            )
        if inv.status in ("awaiting_input", "awaiting_approval"):
            # Nothing is executing; finish immediately and withdraw open approvals.
            withdrawn = await approval_service.cancel_for_investigation(s, inv.id)
            if inv.status == "awaiting_approval" and inv.incident_id is not None:
                await incident_service.workflow_update(
                    s,
                    inv.incident_id,
                    "remediation_cancelled",
                    f"Investigation cancelled by {principal.username}; "
                    f"{withdrawn} pending approval(s) withdrawn",
                    status="investigating",
                    actor=principal.username,
                )
            inv.status = "cancelled"
            inv.pending_question = None
            inv.completed_at = datetime.now(UTC)
            event_type = "investigation.cancelled"
        else:
            # The runner stops at the next node boundary.
            inv.cancel_requested = True
            event_type = "investigation.cancel_requested"
        await events.append_event(
            s,
            tenant_id=principal.tenant_id,
            investigation_id=inv.id,
            event_type=event_type,
            data={"requested_by": principal.username},
        )
        await audit.record(
            s,
            tenant_id=principal.tenant_id,
            actor=principal,
            action="investigation.cancel",
            entity_type="investigation",
            entity_id=inv.id,
            client=client,
        )
        out = InvestigationOut.model_validate(inv)
    await events.notify(redis, principal.tenant_id, investigation_id)
    return out
