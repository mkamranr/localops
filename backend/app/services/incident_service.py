"""Incidents: lifecycle, timeline, linked investigations and automatic creation.

Status changes follow a fixed transition table. ``awaiting_approval`` and
``remediating`` belong to the approval/remediation workflow (phase 5) and cannot be set
by hand. Every change is written to the incident timeline and the audit log.

Discovery opens an incident when a resource becomes unhealthy, de-duplicated by
``dedup_key = resource:<id>:health`` while that incident is open; recovery is noted on
the timeline. Investigations stay user-started, one click away.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from redis.asyncio import Redis
from sqlalchemy import ColumnElement, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ConflictError, NotFoundError
from app.database.session import tenant_session
from app.models import (
    OPEN_STATUSES,
    Incident,
    IncidentEvent,
    IncidentResource,
    Investigation,
    Resource,
    ToolExecution,
    User,
    incident_key,
)
from app.schemas.common import PageParams
from app.schemas.incidents import (
    CommentCreate,
    IncidentClose,
    IncidentCreate,
    IncidentDetail,
    IncidentEventOut,
    IncidentInvestigate,
    IncidentOut,
    IncidentResourceOut,
    IncidentStats,
    IncidentUpdate,
    LinkedInvestigation,
)
from app.services import audit_service as audit
from app.services import knowledge_search
from app.services.context import ClientInfo, Principal, RequestContext

if TYPE_CHECKING:
    from app.graphs.dispatch import InvestigationDispatcher

SEVERITY_PRIORITY = {
    "critical": "P1",
    "high": "P2",
    "medium": "P3",
    "low": "P4",
    "informational": "P4",
}

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "new": ("investigating", "monitoring", "resolved", "closed", "failed"),
    "investigating": (
        "awaiting_approval",
        "remediating",
        "monitoring",
        "resolved",
        "closed",
        "failed",
    ),
    "awaiting_approval": ("investigating", "remediating", "resolved", "closed", "failed"),
    "remediating": ("monitoring", "resolved", "investigating", "failed"),
    "monitoring": ("investigating", "resolved", "closed", "failed"),
    "resolved": ("closed", "investigating"),
    "closed": ("investigating",),
    "failed": ("investigating", "closed"),
}
WORKFLOW_ONLY = frozenset({"awaiting_approval", "remediating"})  # set by phase-5 workflows


class InvalidTransitionError(AppError):
    status_code = 409
    code = "invalid_transition"


def manual_transitions(status: str) -> list[str]:
    return [s for s in TRANSITIONS.get(status, ()) if s not in WORKFLOW_ONLY]


def check_transition(current: str, target: str) -> None:
    if target == current:
        return
    if target in WORKFLOW_ONLY:
        raise InvalidTransitionError(
            f"'{target}' is set by the approval and remediation workflow, not by hand"
        )
    if target not in TRANSITIONS.get(current, ()):
        raise InvalidTransitionError(
            f"An incident cannot move from '{current}' to '{target}'",
            details={"allowed": manual_transitions(current)},
        )


def _apply_status(incident: Incident, target: str) -> None:
    now = datetime.now(UTC)
    incident.status = target
    if target == "resolved":
        incident.resolved_at = incident.resolved_at or now
    elif target == "closed":
        incident.resolved_at = incident.resolved_at or now
        incident.closed_at = now
    elif target in OPEN_STATUSES:  # reopened
        incident.resolved_at = None
        incident.closed_at = None


_last_stamp = datetime.min.replace(tzinfo=UTC)


def _stamp() -> datetime:
    """A strictly increasing timestamp, so events written together keep their order even
    when the clock's resolution is coarse."""
    global _last_stamp
    now = datetime.now(UTC)
    _last_stamp = max(now, _last_stamp + timedelta(microseconds=1))
    return _last_stamp


def _event(
    s: AsyncSession,
    incident: Incident,
    event_type: str,
    summary: str,
    actor: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    s.add(
        IncidentEvent(
            tenant_id=incident.tenant_id,
            incident_id=incident.id,
            event_type=event_type,
            summary=summary[:1000],
            details=details or {},
            actor=actor,
            # Explicit, so events written in one transaction keep their order.
            created_at=_stamp(),
        )
    )


async def _next_number(s: AsyncSession, tenant_id: uuid.UUID) -> int:
    # Serialise numbering per tenant for the rest of the transaction.
    await s.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"incidents:{tenant_id}"))))
    current = await s.scalar(select(func.max(Incident.number)))
    return int(current or 0) + 1


async def _check_resources(s: AsyncSession, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    unique = list(dict.fromkeys(ids))
    if not unique:
        return []
    found = set(await s.scalars(select(Resource.id).where(Resource.id.in_(unique))))
    missing = [str(i) for i in unique if i not in found]
    if missing:
        raise NotFoundError("Resource not found", details={"resource_ids": missing})
    return unique


async def _check_assignee(s: AsyncSession, user_id: uuid.UUID | None) -> None:
    if user_id is None:
        return
    user = await s.get(User, user_id)
    if user is None or user.status == "deleted":
        raise NotFoundError("Assignee not found")


async def _set_resources(s: AsyncSession, incident: Incident, ids: list[uuid.UUID]) -> None:
    await s.execute(delete(IncidentResource).where(IncidentResource.incident_id == incident.id))
    for rid in ids:
        s.add(
            IncidentResource(incident_id=incident.id, resource_id=rid, tenant_id=incident.tenant_id)
        )


async def _resources_of(
    s: AsyncSession, incident_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[IncidentResourceOut]]:
    if not incident_ids:
        return {}
    rows = await s.execute(
        select(IncidentResource.incident_id, Resource)
        .join(Resource, Resource.id == IncidentResource.resource_id)
        .where(IncidentResource.incident_id.in_(incident_ids))
        .order_by(Resource.name)
    )
    out: dict[uuid.UUID, list[IncidentResourceOut]] = {}
    for iid, r in rows.all():
        out.setdefault(iid, []).append(IncidentResourceOut.model_validate(r))
    return out


async def _out(s: AsyncSession, incidents: list[Incident]) -> list[IncidentOut]:
    resources = await _resources_of(s, [i.id for i in incidents])
    assignees = {i.assigned_to for i in incidents if i.assigned_to}
    names = (
        dict((await s.execute(select(User.id, User.username).where(User.id.in_(assignees)))).all())
        if assignees
        else {}
    )
    out = []
    for i in incidents:
        item = IncidentOut.model_validate(i)
        item.resources = resources.get(i.id, [])
        item.assigned_username = names.get(i.assigned_to) if i.assigned_to else None
        out.append(item)
    return out


async def _load(s: AsyncSession, incident_id: uuid.UUID, *, lock: bool = False) -> Incident:
    incident = await s.get(Incident, incident_id, with_for_update=lock)
    if incident is None:
        raise NotFoundError("Incident not found")
    return incident


# ------------------------------------------------------------------------ CRUD


async def create(ctx: RequestContext, data: IncidentCreate) -> IncidentDetail:
    s, p = ctx.session, ctx.principal
    resource_ids = list(data.resource_ids)
    root_cause: dict[str, Any] = {}
    summary: str | None = None
    source = "manual"
    inv: Investigation | None = None
    if data.investigation_id:
        inv = await s.get(Investigation, data.investigation_id)
        if inv is None:
            raise NotFoundError("Investigation not found")
        if inv.incident_id:
            raise ConflictError("That investigation is already linked to an incident")
        result = inv.result or {}
        root_cause = result.get("root_cause") or {}
        summary = result.get("conclusion")
        resource_ids += [uuid.UUID(r) for r in inv.resource_ids or []]
        source = "investigation"
    resource_ids = await _check_resources(s, resource_ids)
    await _check_assignee(s, data.assigned_to)
    incident = Incident(
        tenant_id=p.tenant_id,
        number=await _next_number(s, p.tenant_id),
        title=data.title,
        description=data.description,
        severity=data.severity,
        priority=data.priority or SEVERITY_PRIORITY[data.severity],
        status="investigating" if inv is not None and inv.status != "completed" else "new",
        source=source,
        assigned_to=data.assigned_to,
        created_by=p.username,
        root_cause=root_cause,
        investigation_summary=summary,
    )
    s.add(incident)
    await s.flush()
    await _set_resources(s, incident, resource_ids)
    _event(
        s, incident, "created", f"Incident created by {p.username}", p.username, {"source": source}
    )
    if inv is not None:
        inv.incident_id = incident.id
        _event(
            s,
            incident,
            "investigation_linked",
            f"Linked investigation “{inv.title}”",
            p.username,
            {"investigation_id": str(inv.id)},
        )
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="incident.create",
        entity_type="incident",
        entity_id=incident.id,
        details={
            "number": incident_key(incident.number),
            "severity": incident.severity,
            "source": source,
        },
        client=ctx.client,
    )
    await s.flush()
    return await get(ctx, incident.id)


async def list_incidents(
    ctx: RequestContext,
    page: PageParams,
    *,
    status: str | None = None,
    severity: str | None = None,
    source: str | None = None,
    assigned_to: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    open_only: bool | None = None,
    q: str | None = None,
) -> tuple[list[IncidentOut], int]:
    query = select(Incident)
    if status:
        query = query.where(Incident.status == status)
    if open_only:
        query = query.where(Incident.status.in_(OPEN_STATUSES))
    if severity:
        query = query.where(Incident.severity == severity)
    if source:
        query = query.where(Incident.source == source)
    if assigned_to:
        query = query.where(Incident.assigned_to == assigned_to)
    if resource_id:
        query = query.where(
            Incident.id.in_(
                select(IncidentResource.incident_id).where(
                    IncidentResource.resource_id == resource_id
                )
            )
        )
    if q:
        text = q.strip()
        number = text.upper().removeprefix("INC-").lstrip("0")
        like = f"%{text}%"
        conditions: list[ColumnElement[bool]] = [
            Incident.title.ilike(like),
            Incident.description.ilike(like),
        ]
        if number.isdigit():
            conditions.append(Incident.number == int(number))
        query = query.where(or_(*conditions))
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(
        await ctx.session.scalars(
            query.order_by(Incident.created_at.desc()).offset(page.offset).limit(page.page_size)
        )
    )
    return await _out(ctx.session, rows), total


async def get(ctx: RequestContext, incident_id: uuid.UUID) -> IncidentDetail:
    s = ctx.session
    incident = await _load(s, incident_id)
    base = (await _out(s, [incident]))[0]
    invs = await s.scalars(
        select(Investigation)
        .where(Investigation.incident_id == incident.id)
        .order_by(Investigation.started_at.desc())
    )
    linked = [
        LinkedInvestigation(
            id=i.id,
            title=i.title,
            status=i.status,
            started_at=i.started_at,
            completed_at=i.completed_at,
            conclusion=(i.result or {}).get("conclusion"),
        )
        for i in invs
    ]
    return IncidentDetail(
        **base.model_dump(exclude={"key"}),
        investigations=linked,
        allowed_transitions=manual_transitions(incident.status),
    )


async def update(
    ctx: RequestContext, incident_id: uuid.UUID, data: IncidentUpdate
) -> IncidentDetail:
    s, p = ctx.session, ctx.principal
    incident = await _load(s, incident_id, lock=True)
    fields = data.model_fields_set
    changes: dict[str, Any] = {}
    for name in ("title", "description", "severity", "priority", "resolution"):
        value = getattr(data, name)
        if name in fields and value is not None and value != getattr(incident, name):
            changes[name] = {"from": getattr(incident, name), "to": value}
            setattr(incident, name, value)
    if "severity" in changes and "priority" not in fields:
        priority = SEVERITY_PRIORITY[incident.severity]
        if priority != incident.priority:
            changes["priority"] = {"from": incident.priority, "to": priority}
            incident.priority = priority
    if changes:
        _event(
            s,
            incident,
            "updated",
            "Updated " + ", ".join(changes),
            p.username,
            {"changes": changes},
        )
    if "assigned_to" in fields and data.assigned_to != incident.assigned_to:
        await _check_assignee(s, data.assigned_to)
        assignee = (
            await s.scalar(select(User.username).where(User.id == data.assigned_to))
            if data.assigned_to
            else None
        )
        incident.assigned_to = data.assigned_to
        _event(
            s,
            incident,
            "assigned",
            f"Assigned to {assignee}" if assignee else "Unassigned",
            p.username,
            {"assigned_to": str(data.assigned_to) if data.assigned_to else None},
        )
        changes["assigned_to"] = assignee
    if data.resource_ids is not None:
        ids = await _check_resources(s, data.resource_ids)
        await _set_resources(s, incident, ids)
        _event(s, incident, "resources_changed", f"{len(ids)} affected resource(s)", p.username)
        changes["resources"] = len(ids)
    if data.status and data.status != incident.status:
        check_transition(incident.status, data.status)
        previous = incident.status
        _apply_status(incident, data.status)
        _event(
            s,
            incident,
            "status_changed",
            f"Status {previous} → {data.status}",
            p.username,
            {"from": previous, "to": data.status},
        )
        changes["status"] = {"from": previous, "to": data.status}
    if changes:
        incident.updated_at = datetime.now(UTC)
        await audit.record(
            s,
            tenant_id=p.tenant_id,
            actor=p,
            action="incident.update",
            entity_type="incident",
            entity_id=incident.id,
            details={"number": incident_key(incident.number), "changes": changes},
            client=ctx.client,
        )
    await s.flush()
    return await get(ctx, incident.id)


async def close(ctx: RequestContext, incident_id: uuid.UUID, data: IncidentClose) -> IncidentDetail:
    s, p = ctx.session, ctx.principal
    incident = await _load(s, incident_id, lock=True)
    if incident.status != data.status:
        check_transition(incident.status, data.status)
    previous = incident.status
    incident.resolution = data.resolution
    _apply_status(incident, data.status)
    incident.updated_at = datetime.now(UTC)
    _event(
        s,
        incident,
        "status_changed",
        f"{data.status.title()}: {data.resolution[:200]}",
        p.username,
        {"from": previous, "to": data.status, "resolution": data.resolution},
    )
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="incident.close",
        entity_type="incident",
        entity_id=incident.id,
        details={"number": incident_key(incident.number), "status": data.status},
        client=ctx.client,
    )
    await s.flush()
    return await get(ctx, incident.id)


async def comment(
    ctx: RequestContext, incident_id: uuid.UUID, data: CommentCreate
) -> IncidentEventOut:
    s, p = ctx.session, ctx.principal
    incident = await _load(s, incident_id)
    row = IncidentEvent(
        tenant_id=incident.tenant_id,
        incident_id=incident.id,
        event_type="comment",
        summary=data.body.strip()[:1000],
        details={"body": data.body.strip()} if len(data.body.strip()) > 1000 else {},
        actor=p.username,
    )
    s.add(row)
    incident.updated_at = datetime.now(UTC)
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="incident.comment",
        entity_type="incident",
        entity_id=incident.id,
        client=ctx.client,
    )
    await s.flush()
    await s.refresh(row)
    return IncidentEventOut.model_validate(row)


async def timeline(ctx: RequestContext, incident_id: uuid.UUID) -> list[IncidentEventOut]:
    await _load(ctx.session, incident_id)
    rows = await ctx.session.scalars(
        select(IncidentEvent)
        .where(IncidentEvent.incident_id == incident_id)
        .order_by(IncidentEvent.created_at, IncidentEvent.id)
        .limit(2000)
    )
    return [IncidentEventOut.model_validate(r) for r in rows]


async def similar(ctx: RequestContext, incident_id: uuid.UUID) -> list[dict[str, Any]]:
    s = ctx.session
    incident = await _load(s, incident_id)
    ids = list(
        await s.scalars(
            select(IncidentResource.resource_id).where(IncidentResource.incident_id == incident.id)
        )
    )
    text = f"{incident.title} {incident.description}"
    incidents = await knowledge_search.similar_incidents(
        s, text, ids, exclude_id=incident.id, limit=8
    )
    linked = select(Investigation.id).where(Investigation.incident_id == incident.id)
    investigations = [
        i
        for i in await knowledge_search.similar_investigations(s, text, ids, limit=8)
        if uuid.UUID(i["id"]) not in set(await s.scalars(linked))
    ]
    return sorted([*incidents, *investigations], key=lambda x: -x["score"])[:10]


async def tool_executions(ctx: RequestContext, incident_id: uuid.UUID) -> list[ToolExecution]:
    await _load(ctx.session, incident_id)
    rows = await ctx.session.scalars(
        select(ToolExecution)
        .join(Investigation, Investigation.id == ToolExecution.investigation_id)
        .where(Investigation.incident_id == incident_id)
        .order_by(ToolExecution.created_at)
        .limit(500)
    )
    return list(rows)


async def stats(ctx: RequestContext) -> IncidentStats:
    s = ctx.session
    by_status = dict(
        (await s.execute(select(Incident.status, func.count()).group_by(Incident.status))).all()
    )
    by_severity = dict(
        (
            await s.execute(
                select(Incident.severity, func.count())
                .where(Incident.status.in_(OPEN_STATUSES))
                .group_by(Incident.severity)
            )
        ).all()
    )
    since = datetime.now(UTC) - timedelta(days=7)
    resolved = await s.scalar(
        select(func.count()).select_from(Incident).where(Incident.resolved_at >= since)
    )
    mttr = await s.scalar(
        select(func.avg(func.extract("epoch", Incident.resolved_at - Incident.created_at))).where(
            Incident.resolved_at.is_not(None),
            Incident.resolved_at >= datetime.now(UTC) - timedelta(days=30),
        )
    )
    return IncidentStats(
        open=sum(v for k, v in by_status.items() if k in OPEN_STATUSES),
        by_status={k: int(v) for k, v in by_status.items()},
        by_severity_open={k: int(v) for k, v in by_severity.items()},
        resolved_last_7d=int(resolved or 0),
        mean_time_to_resolve_minutes=round(float(mttr) / 60, 1) if mttr is not None else None,
    )


async def open_counts(s: AsyncSession) -> dict[str, int]:
    rows = await s.execute(
        select(Incident.severity, func.count())
        .where(Incident.status.in_(OPEN_STATUSES))
        .group_by(Incident.severity)
    )
    return {k: int(v) for k, v in rows.all()}


# --------------------------------------------------------------- investigations


def default_request(incident: Incident, resource_names: list[str]) -> str:
    parts = [f"Investigate incident {incident_key(incident.number)}: {incident.title}."]
    if incident.description:
        parts.append(incident.description.strip()[:2000])
    if resource_names:
        parts.append("Affected resources: " + ", ".join(resource_names) + ".")
    return "\n".join(parts)


async def investigate(
    principal: Principal,
    client: ClientInfo,
    incident_id: uuid.UUID,
    data: IncidentInvestigate,
    dispatcher: InvestigationDispatcher,
) -> Investigation:
    from app.schemas.ai import InvestigationCreate
    from app.services import investigation_service

    async with tenant_session(principal.tenant_id) as s:
        incident = await _load(s, incident_id)
        if incident.status not in OPEN_STATUSES and incident.status != "failed":
            raise ConflictError(
                "Reopen the incident before investigating it", details={"status": incident.status}
            )
        rows = (
            await s.execute(
                select(Resource.id, Resource.name)
                .join(IncidentResource, IncidentResource.resource_id == Resource.id)
                .where(IncidentResource.incident_id == incident.id)
            )
        ).all()
        request = (data.request or "").strip() or default_request(incident, [r[1] for r in rows])
        title = f"{incident_key(incident.number)}: {incident.title}"[:200]
    body = InvestigationCreate(
        request=request[:4000],
        title=title,
        execution_mode=data.execution_mode,
        resource_ids=[r[0] for r in rows],
        incident_id=incident_id,
        investigation_type="incident_investigation",
    )
    return await investigation_service.create(principal, client, body, dispatcher)


async def link_investigation(
    s: AsyncSession, incident_id: uuid.UUID, investigation: Investigation, actor: str
) -> None:
    """Called while the investigation is created (same transaction)."""
    incident = await _load(s, incident_id, lock=True)
    if incident.status in ("new", "monitoring", "failed", "resolved", "closed"):
        previous = incident.status
        _apply_status(incident, "investigating")
        _event(
            s,
            incident,
            "status_changed",
            f"Status {previous} → investigating",
            actor,
            {"from": previous, "to": "investigating"},
        )
    incident.updated_at = datetime.now(UTC)
    _event(
        s,
        incident,
        "investigation_started",
        f"Investigation started: {investigation.title}",
        actor,
        {"investigation_id": str(investigation.id)},
    )


async def record_investigation_result(
    tenant_id: uuid.UUID,
    investigation_id: uuid.UUID,
    report: dict[str, Any] | None,
    *,
    redis: Redis | None = None,
    error: str | None = None,
) -> None:
    """Write an investigation's outcome back to its incident (if any)."""
    async with tenant_session(tenant_id) as s:
        inv = await s.get(Investigation, investigation_id)
        if inv is None or inv.incident_id is None:
            return
        incident = await s.get(Incident, inv.incident_id, with_for_update=True)
        if incident is None:
            return
        if report is None:
            _event(
                s,
                incident,
                "investigation_failed",
                f"Investigation failed: {(error or 'unknown error')[:300]}",
                "system",
                {"investigation_id": str(inv.id)},
            )
            return
        root = report.get("root_cause")
        if root:
            incident.root_cause = {
                **root,
                "investigation_id": str(inv.id),
                "contributing_factors": report.get("contributing_factors", []),
                "recommended_remediation": report.get("recommended_remediation", []),
            }
        incident.investigation_summary = report.get("conclusion")
        incident.updated_at = datetime.now(UTC)
        triage = report.get("triage") or {}
        _event(
            s,
            incident,
            "investigation_completed",
            report.get("conclusion") or "Investigation completed",
            "system",
            {
                "investigation_id": str(inv.id),
                "root_cause_status": root.get("status") if root else None,
                "confidence": report.get("confidence"),
                "suggested_severity": triage.get("severity"),
                "suggested_priority": triage.get("priority"),
            },
        )


async def workflow_update(
    s: AsyncSession,
    incident_id: uuid.UUID | None,
    event_type: str,
    summary: str,
    *,
    status: str | None = None,
    actor: str | None = "system",
    details: dict[str, Any] | None = None,
) -> None:
    """Record a remediation-workflow step on an incident and, when the transition table
    allows it, move the incident (including the workflow-only statuses)."""
    if incident_id is None:
        return
    incident = await s.get(Incident, incident_id, with_for_update=True)
    if incident is None:
        return
    moved: dict[str, str] = {}
    if status and status != incident.status:
        if incident.status == "new" and status != "investigating":
            # The workflow always passes through "investigating" first.
            _apply_status(incident, "investigating")
        if status in TRANSITIONS.get(incident.status, ()):
            moved = {"from": incident.status, "to": status}
            _apply_status(incident, status)
            incident.updated_at = datetime.now(UTC)
    _event(s, incident, event_type, summary, actor, {**(details or {}), **moved})


# ---------------------------------------------------------------- automatic


@dataclass(slots=True)
class HealthChange:
    resource: Resource
    previous: str | None  # None: newly discovered
    current: str
    reasons: list[str]


def health_dedup_key(resource_id: uuid.UUID) -> str:
    return f"resource:{resource_id}:health"


def _auto_severity(resource: Resource) -> str:
    prod = (resource.environment or "").lower() in ("production", "prod", "prd", "live")
    return "high" if prod else "medium"


async def on_health_changes(
    s: AsyncSession, tenant_id: uuid.UUID, changes: list[HealthChange], *, create: bool = True
) -> int:
    """Open (or annotate) incidents for health transitions. Returns incidents opened."""
    opened = 0
    for change in changes:
        r = change.resource
        key = health_dedup_key(r.id)
        existing = await s.scalar(
            select(Incident).where(Incident.dedup_key == key, Incident.status.in_(OPEN_STATUSES))
        )
        reasons = "; ".join(change.reasons) or "no reason recorded"
        if change.current == "unhealthy":
            if existing is not None:
                _event(
                    s,
                    existing,
                    "resource_health",
                    f"{r.name} is still unhealthy: {reasons}"[:1000],
                    "discovery",
                    {"resource_id": str(r.id), "from": change.previous, "to": change.current},
                )
                continue
            if not create:
                continue
            incident = Incident(
                tenant_id=tenant_id,
                number=await _next_number(s, tenant_id),
                title=f"{r.name} is unhealthy"[:200],
                description=(
                    f"Discovery reported {r.name} ({r.resource_type}) as unhealthy: {reasons}"
                ),
                severity=_auto_severity(r),
                priority=SEVERITY_PRIORITY[_auto_severity(r)],
                status="new",
                source="discovery",
                dedup_key=key,
                created_by="discovery",
            )
            s.add(incident)
            await s.flush()
            s.add(IncidentResource(incident_id=incident.id, resource_id=r.id, tenant_id=tenant_id))
            _event(
                s,
                incident,
                "created",
                f"Opened automatically: {r.name} became unhealthy",
                "discovery",
                {"resource_id": str(r.id), "from": change.previous, "reasons": change.reasons},
            )
            await audit.record(
                s,
                tenant_id=tenant_id,
                action="incident.create",
                username="system",
                entity_type="incident",
                entity_id=incident.id,
                details={
                    "number": incident_key(incident.number),
                    "source": "discovery",
                    "resource": r.name,
                },
            )
            opened += 1
        elif existing is not None and change.previous == "unhealthy":
            _event(
                s,
                existing,
                "resource_health",
                f"{r.name} health {change.previous} → {change.current}",
                "discovery",
                {"resource_id": str(r.id), "from": change.previous, "to": change.current},
            )
    return opened
