"""Incident management endpoints (spec §10.4)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import (
    client_info,
    dispatcher_dep,
    require_permission,
    require_principal,
)
from app.graphs.dispatch import InvestigationDispatcher
from app.schemas.ai import InvestigationOut
from app.schemas.common import Page, PageParams
from app.schemas.incidents import (
    CommentCreate,
    IncidentClose,
    IncidentCreate,
    IncidentDetail,
    IncidentEventOut,
    IncidentInvestigate,
    IncidentOut,
    IncidentSource,
    IncidentStats,
    IncidentStatus,
    IncidentUpdate,
    Severity,
    SimilarIncident,
    ToolExecutionOut,
)
from app.services import incident_service as svc
from app.services.context import ClientInfo, Principal, RequestContext

router = APIRouter(prefix="/incidents", tags=["incidents"])


@router.get("", response_model=Page[IncidentOut])
async def list_incidents(
    page: PageParams = Depends(),
    status: IncidentStatus | None = None,
    severity: Severity | None = None,
    source: IncidentSource | None = None,
    assigned_to: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    open: bool | None = Query(None, description="Only open incidents"),
    q: str | None = Query(None, max_length=200),
    ctx: RequestContext = Depends(require_permission("incident.read")),
) -> Page[IncidentOut]:
    rows, total = await svc.list_incidents(
        ctx,
        page,
        status=status,
        severity=severity,
        source=source,
        assigned_to=assigned_to,
        resource_id=resource_id,
        open_only=open,
        q=q,
    )
    return Page(items=rows, total=total, page=page.page, page_size=page.page_size)


@router.get("/summary", response_model=IncidentStats)
async def incident_summary(
    ctx: RequestContext = Depends(require_permission("incident.read")),
) -> IncidentStats:
    return await svc.stats(ctx)


@router.post("", response_model=IncidentDetail, status_code=201)
async def create_incident(
    body: IncidentCreate,
    ctx: RequestContext = Depends(require_permission("incident.create")),
) -> IncidentDetail:
    return await svc.create(ctx, body)


@router.get("/{incident_id}", response_model=IncidentDetail)
async def get_incident(
    incident_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("incident.read")),
) -> IncidentDetail:
    return await svc.get(ctx, incident_id)


@router.put("/{incident_id}", response_model=IncidentDetail)
async def update_incident(
    incident_id: uuid.UUID,
    body: IncidentUpdate,
    ctx: RequestContext = Depends(require_permission("incident.update")),
) -> IncidentDetail:
    return await svc.update(ctx, incident_id, body)


@router.post("/{incident_id}/close", response_model=IncidentDetail)
async def close_incident(
    incident_id: uuid.UUID,
    body: IncidentClose,
    ctx: RequestContext = Depends(require_permission("incident.update")),
) -> IncidentDetail:
    return await svc.close(ctx, incident_id, body)


@router.post("/{incident_id}/investigate", response_model=InvestigationOut, status_code=202)
async def investigate_incident(
    incident_id: uuid.UUID,
    body: IncidentInvestigate | None = None,
    client: ClientInfo = Depends(client_info),
    dispatcher: InvestigationDispatcher = Depends(dispatcher_dep),
    principal: Principal = Depends(require_principal("incident.investigate")),
) -> InvestigationOut:
    inv = await svc.investigate(
        principal, client, incident_id, body or IncidentInvestigate(), dispatcher
    )
    return InvestigationOut.model_validate(inv)


@router.get("/{incident_id}/timeline", response_model=list[IncidentEventOut])
async def incident_timeline(
    incident_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("incident.read")),
) -> list[IncidentEventOut]:
    return await svc.timeline(ctx, incident_id)


@router.post("/{incident_id}/comments", response_model=IncidentEventOut, status_code=201)
async def add_comment(
    incident_id: uuid.UUID,
    body: CommentCreate,
    ctx: RequestContext = Depends(require_permission("incident.update")),
) -> IncidentEventOut:
    return await svc.comment(ctx, incident_id, body)


@router.get("/{incident_id}/similar", response_model=list[SimilarIncident])
async def similar_incidents(
    incident_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("incident.read")),
) -> list[SimilarIncident]:
    return [SimilarIncident.model_validate(i) for i in await svc.similar(ctx, incident_id)]


@router.get("/{incident_id}/tool-executions", response_model=list[ToolExecutionOut])
async def incident_tool_executions(
    incident_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("incident.read")),
) -> list[ToolExecutionOut]:
    return [ToolExecutionOut.model_validate(r) for r in await svc.tool_executions(ctx, incident_id)]
