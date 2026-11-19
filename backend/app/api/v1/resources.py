"""Resource inventory, health, history, metrics, relationships and dependency graphs."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response

from app.api.dependencies import (
    client_info,
    dispatcher_dep,
    require_permission,
    require_principal,
)
from app.graphs.dispatch import InvestigationDispatcher
from app.schemas.common import Page, PageParams
from app.schemas.inventory import (
    DependencyGraph,
    DiscoverRequest,
    DiscoveryRunOut,
    HealthOut,
    HealthStatus,
    InventorySummary,
    MetricSampleOut,
    RelationshipCreate,
    RelationshipOut,
    ResourceCreate,
    ResourceEventOut,
    ResourceOut,
    ResourceSummaryOut,
    ResourceType,
    ResourceUpdate,
)
from app.services import integration_service
from app.services import resource_service as svc
from app.services.context import ClientInfo, Principal, RequestContext

router = APIRouter(tags=["resources"])


@router.get("/resources", response_model=Page[ResourceSummaryOut])
async def list_resources(
    page: PageParams = Depends(),
    q: str | None = Query(None, max_length=200),
    resource_type: ResourceType | None = Query(None, alias="type"),
    environment: str | None = Query(None, max_length=50),
    health: HealthStatus | None = None,
    group: str | None = Query(None, max_length=100),
    tag: str | None = Query(None, max_length=50),
    integration_id: uuid.UUID | None = None,
    stale: bool | None = None,
    ctx: RequestContext = Depends(require_permission("resource.read")),
) -> Page[ResourceSummaryOut]:
    rows, total = await svc.list_resources(
        ctx.session,
        page,
        q=q,
        resource_type=resource_type,
        environment=environment,
        health=health,
        group=group,
        tag=tag,
        integration_id=integration_id,
        stale=stale,
    )
    return Page(
        items=[ResourceSummaryOut.model_validate(r) for r in rows],
        total=total,
        page=page.page,
        page_size=page.page_size,
    )


@router.post("/resources", response_model=ResourceOut, status_code=201)
async def create_resource(
    body: ResourceCreate,
    ctx: RequestContext = Depends(require_permission("resource.create")),
) -> ResourceOut:
    return await svc.resource_out(ctx.session, await svc.create_resource(ctx, body))


@router.get("/resources/summary", response_model=InventorySummary)
async def inventory_summary(
    ctx: RequestContext = Depends(require_permission("resource.read")),
) -> InventorySummary:
    return await svc.summary(ctx.session)


@router.get("/resources/facets")
async def facets(
    ctx: RequestContext = Depends(require_permission("resource.read")),
) -> dict[str, list[str]]:
    return await svc.environments_and_groups(ctx.session)


@router.get("/resources/graph", response_model=DependencyGraph)
async def topology(
    environment: str | None = Query(None, max_length=50),
    group: str | None = Query(None, max_length=100),
    resource_type: ResourceType | None = Query(None, alias="type"),
    include_stale: bool = False,
    ctx: RequestContext = Depends(require_permission("resource.read")),
) -> DependencyGraph:
    return await svc.graph(
        ctx.session,
        environment=environment,
        group=group,
        resource_type=resource_type,
        include_stale=include_stale,
    )


@router.post("/resources/discover", response_model=DiscoveryRunOut, status_code=202)
async def discover(
    body: DiscoverRequest,
    client: ClientInfo = Depends(client_info),
    dispatcher: InvestigationDispatcher = Depends(dispatcher_dep),
    principal: Principal = Depends(require_principal("resource.create", "integration.read")),
) -> DiscoveryRunOut:
    run = await integration_service.start_discovery(
        principal, client, body.integration_id, dispatcher
    )
    return DiscoveryRunOut.model_validate(run)


@router.get("/resources/{resource_id}", response_model=ResourceOut)
async def get_resource(
    resource_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("resource.read")),
) -> ResourceOut:
    return await svc.resource_out(ctx.session, await svc.get_resource(ctx.session, resource_id))


@router.put("/resources/{resource_id}", response_model=ResourceOut)
async def update_resource(
    resource_id: uuid.UUID,
    body: ResourceUpdate,
    ctx: RequestContext = Depends(require_permission("resource.update")),
) -> ResourceOut:
    resource = await svc.update_resource(ctx, resource_id, body)
    return await svc.resource_out(ctx.session, resource)


@router.delete("/resources/{resource_id}", status_code=204)
async def delete_resource(
    resource_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("resource.delete")),
) -> Response:
    await svc.delete_resource(ctx, resource_id)
    return Response(status_code=204)


@router.get("/resources/{resource_id}/health", response_model=HealthOut)
async def resource_health(
    resource_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("resource.read")),
) -> HealthOut:
    r = await svc.get_resource(ctx.session, resource_id)
    return HealthOut(
        resource_id=r.id,
        health_status=r.health_status,
        health_reasons=r.health_reasons,
        last_health_at=r.last_health_at,
        stale=r.stale,
        last_seen_at=r.last_seen_at,
    )


@router.post(
    "/resources/{resource_id}/health/check", response_model=DiscoveryRunOut, status_code=202
)
async def check_health(
    resource_id: uuid.UUID,
    client: ClientInfo = Depends(client_info),
    dispatcher: InvestigationDispatcher = Depends(dispatcher_dep),
    principal: Principal = Depends(require_principal("resource.update")),
) -> DiscoveryRunOut:
    run = await svc.check_health(principal, client, resource_id, dispatcher)
    return DiscoveryRunOut.model_validate(run)


@router.get("/resources/{resource_id}/history", response_model=Page[ResourceEventOut])
async def resource_history(
    resource_id: uuid.UUID,
    page: PageParams = Depends(),
    ctx: RequestContext = Depends(require_permission("resource.read")),
) -> Page[ResourceEventOut]:
    rows, total = await svc.history(ctx.session, resource_id, page)
    return Page(
        items=[ResourceEventOut.model_validate(r) for r in rows],
        total=total,
        page=page.page,
        page_size=page.page_size,
    )


@router.get("/resources/{resource_id}/metrics", response_model=list[MetricSampleOut])
async def resource_metrics(
    resource_id: uuid.UUID,
    hours: int = Query(168, ge=1, le=24 * 90),
    ctx: RequestContext = Depends(require_permission("resource.read")),
) -> list[MetricSampleOut]:
    rows = await svc.metrics(ctx.session, resource_id, hours)
    return [MetricSampleOut.model_validate(r) for r in rows]


@router.get("/resources/{resource_id}/relationships", response_model=list[RelationshipOut])
async def resource_relationships(
    resource_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("resource.read")),
) -> list[RelationshipOut]:
    rows = await svc.list_relationships(ctx.session, resource_id)
    return [RelationshipOut.model_validate(r) for r in rows]


@router.get("/resources/{resource_id}/dependencies", response_model=DependencyGraph)
async def resource_dependencies(
    resource_id: uuid.UUID,
    direction: Literal["up", "down", "both"] = "both",
    depth: int = Query(3, ge=1, le=6),
    ctx: RequestContext = Depends(require_permission("resource.read")),
) -> DependencyGraph:
    return await svc.dependencies(ctx.session, resource_id, direction, depth)


@router.post("/resource-relationships", response_model=RelationshipOut, status_code=201)
async def create_relationship(
    body: RelationshipCreate,
    ctx: RequestContext = Depends(require_permission("resource.update")),
) -> RelationshipOut:
    return RelationshipOut.model_validate(await svc.create_relationship(ctx, body))


@router.delete("/resource-relationships/{relationship_id}", status_code=204)
async def delete_relationship(
    relationship_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("resource.update")),
) -> Response:
    await svc.delete_relationship(ctx, relationship_id)
    return Response(status_code=204)
