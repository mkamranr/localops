"""Resource inventory (spec §10.3): list/filter, CRUD, history, metrics, relationships,
dependency traversal (recursive CTE) and the tenant topology graph."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Text, cast, func, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.database.session import tenant_session
from app.graphs.dispatch import InvestigationDispatcher
from app.models import (
    DiscoveryRun,
    Integration,
    Resource,
    ResourceEvent,
    ResourceMetricSample,
    ResourceRelationship,
)
from app.schemas.common import PageParams
from app.schemas.inventory import (
    DependencyGraph,
    GraphEdge,
    GraphNode,
    InventorySummary,
    RelationshipCreate,
    ResourceCreate,
    ResourceOut,
    ResourceUpdate,
)
from app.services import audit_service as audit
from app.services import integration_service
from app.services.context import ClientInfo, Principal, RequestContext

GRAPH_LIMIT = 500
# Relationship semantics for dependency traversal: (dependent, dependency).
FORWARD = ("DEPENDS_ON", "RUNS_ON", "CONNECTS_TO", "STORES_DATA_IN")  # source depends on target
INVERSE = ("HOSTS", "EXPOSES")  # target depends on source
USER_FIELDS = (
    "name",
    "resource_type",
    "hostname",
    "ip_addresses",
    "os",
    "environment",
    "group_name",
    "owner",
    "tags",
    "description",
)


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def list_resources(
    session: AsyncSession,
    page: PageParams,
    *,
    q: str | None = None,
    resource_type: str | None = None,
    environment: str | None = None,
    health: str | None = None,
    group: str | None = None,
    tag: str | None = None,
    integration_id: uuid.UUID | None = None,
    stale: bool | None = None,
) -> tuple[list[Resource], int]:
    stmt = select(Resource)
    if q:
        pattern = _like(q.strip())
        stmt = stmt.where(
            or_(
                Resource.name.ilike(pattern, escape="\\"),
                Resource.hostname.ilike(pattern, escape="\\"),
                cast(Resource.ip_addresses, Text).ilike(pattern, escape="\\"),
                Resource.description.ilike(pattern, escape="\\"),
            )
        )
    if resource_type:
        stmt = stmt.where(Resource.resource_type == resource_type)
    if environment:
        stmt = stmt.where(Resource.environment == environment)
    if health:
        stmt = stmt.where(Resource.health_status == health)
    if group:
        stmt = stmt.where(Resource.group_name == group)
    if tag:
        stmt = stmt.where(Resource.tags.contains(cast([tag], JSONB)))
    if integration_id:
        stmt = stmt.where(Resource.integration_id == integration_id)
    if stale is not None:
        stmt = stmt.where(Resource.stale.is_(stale))
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = await session.scalars(
        stmt.order_by(Resource.name, Resource.id).offset(page.offset).limit(page.page_size)
    )
    return list(rows), int(total or 0)


async def summary(session: AsyncSession) -> InventorySummary:
    health_rows = await session.execute(
        select(Resource.health_status, func.count()).group_by(Resource.health_status)
    )
    type_rows = await session.execute(
        select(Resource.resource_type, func.count()).group_by(Resource.resource_type)
    )
    by_health: dict[str, int] = {r[0]: int(r[1]) for r in health_rows.all()}
    by_type: dict[str, int] = {r[0]: int(r[1]) for r in type_rows.all()}
    stale = await session.scalar(
        select(func.count()).select_from(Resource).where(Resource.stale.is_(True))
    )
    last = await session.scalar(select(func.max(DiscoveryRun.completed_at)))
    integrations = await session.scalar(select(func.count()).select_from(Integration))
    return InventorySummary(
        total=sum(by_health.values()),
        by_health=by_health,
        by_type=by_type,
        stale=int(stale or 0),
        last_discovery_at=last,
        integrations=int(integrations or 0),
    )


async def get_resource(session: AsyncSession, resource_id: uuid.UUID) -> Resource:
    resource = await session.get(Resource, resource_id)
    if resource is None:
        raise NotFoundError("Resource not found")
    return resource


async def resource_out(session: AsyncSession, resource: Resource) -> ResourceOut:
    out = ResourceOut.model_validate(resource)
    if resource.integration_id:
        out.integration_name = await session.scalar(
            select(Integration.name).where(Integration.id == resource.integration_id)
        )
    return out


def _event(
    resource: Resource, event_type: str, summary_text: str, actor: str, details: dict[str, Any]
) -> ResourceEvent:
    return ResourceEvent(
        tenant_id=resource.tenant_id,
        resource_id=resource.id,
        event_type=event_type,
        summary=summary_text[:500],
        details=details,
        actor=actor,
    )


async def create_resource(ctx: RequestContext, data: ResourceCreate) -> Resource:
    s, p = ctx.session, ctx.principal
    resource = Resource(
        tenant_id=p.tenant_id,
        source="manual",
        health_status="unknown",
        **{f: getattr(data, f) for f in USER_FIELDS},
    )
    s.add(resource)
    await s.flush()
    s.add(_event(resource, "created", f"Registered manually by {p.username}", p.username, {}))
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="resource.create",
        entity_type="resource",
        entity_id=resource.id,
        details={"name": resource.name, "type": resource.resource_type},
        client=ctx.client,
    )
    return resource


async def update_resource(
    ctx: RequestContext, resource_id: uuid.UUID, data: ResourceUpdate
) -> Resource:
    s, p = ctx.session, ctx.principal
    resource = await get_resource(s, resource_id)
    changes: dict[str, Any] = {}
    for field in USER_FIELDS:
        if field not in data.model_fields_set:
            continue
        value = getattr(data, field)
        if value is None and field in ("name", "resource_type", "description"):
            continue  # required fields cannot be cleared
        if value is None and field in ("ip_addresses", "tags"):
            value = []
        if value != getattr(resource, field):
            changes[field] = {"from": getattr(resource, field), "to": value}
            setattr(resource, field, value)
    if changes:
        await s.flush()
        s.add(
            _event(
                resource,
                "updated",
                f"Edited by {p.username}: {', '.join(changes)}",
                p.username,
                {"changes": changes},
            )
        )
        await audit.record(
            s,
            tenant_id=p.tenant_id,
            actor=p,
            action="resource.update",
            entity_type="resource",
            entity_id=resource.id,
            details={"name": resource.name, "changes": list(changes)},
            client=ctx.client,
        )
    return resource


async def delete_resource(ctx: RequestContext, resource_id: uuid.UUID) -> None:
    s, p = ctx.session, ctx.principal
    resource = await get_resource(s, resource_id)
    await s.delete(resource)
    await s.flush()
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="resource.delete",
        entity_type="resource",
        entity_id=resource_id,
        details={"name": resource.name, "type": resource.resource_type, "source": resource.source},
        client=ctx.client,
    )


async def history(
    session: AsyncSession, resource_id: uuid.UUID, page: PageParams
) -> tuple[list[ResourceEvent], int]:
    await get_resource(session, resource_id)
    base = select(ResourceEvent).where(ResourceEvent.resource_id == resource_id)
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = await session.scalars(
        base.order_by(ResourceEvent.created_at.desc(), ResourceEvent.id.desc())
        .offset(page.offset)
        .limit(page.page_size)
    )
    return list(rows), int(total or 0)


async def metrics(
    session: AsyncSession, resource_id: uuid.UUID, hours: int
) -> list[ResourceMetricSample]:
    await get_resource(session, resource_id)
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = await session.scalars(
        select(ResourceMetricSample)
        .where(
            ResourceMetricSample.resource_id == resource_id, ResourceMetricSample.taken_at >= since
        )
        .order_by(ResourceMetricSample.taken_at)
        .limit(2000)
    )
    return list(rows)


async def check_health(
    principal: Principal,
    client: ClientInfo,
    resource_id: uuid.UUID,
    dispatcher: InvestigationDispatcher,
) -> DiscoveryRun:
    """Re-collect just this resource's target (a one-target discovery run)."""
    async with tenant_session(principal.tenant_id) as s:
        resource = await get_resource(s, resource_id)
        if resource.integration_id is None or not resource.discovery_target:
            raise ConflictError(
                "This resource is not discovered by an integration, so it cannot be checked"
            )
        integration = await integration_service.get_integration(s, resource.integration_id)
        if not integration.enabled:
            raise ConflictError("The resource's integration is disabled")
        if await integration_service.active_run(s, integration.id):
            raise ConflictError("A discovery run for this integration is already in progress")
        run = await integration_service.create_run(
            s, integration, trigger="manual", triggered_by=principal.user_id
        )
        run.stats = {"only_target": resource.discovery_target}
        await audit.record(
            s,
            tenant_id=principal.tenant_id,
            actor=principal,
            action="resource.health_check",
            entity_type="resource",
            entity_id=resource.id,
            details={"name": resource.name, "run_id": str(run.id)},
            client=client,
        )
    await dispatcher.enqueue_discovery(principal.tenant_id, run.id)
    return run


# ------------------------------------------------------------------ relationships


async def list_relationships(
    session: AsyncSession, resource_id: uuid.UUID
) -> list[ResourceRelationship]:
    await get_resource(session, resource_id)
    rows = await session.scalars(
        select(ResourceRelationship)
        .where(
            or_(
                ResourceRelationship.source_id == resource_id,
                ResourceRelationship.target_id == resource_id,
            )
        )
        .order_by(ResourceRelationship.relationship_type)
    )
    return list(rows)


async def create_relationship(
    ctx: RequestContext, data: RelationshipCreate
) -> ResourceRelationship:
    s, p = ctx.session, ctx.principal
    source = await get_resource(s, data.source_id)
    target = await get_resource(s, data.target_id)
    existing = await s.scalar(
        select(ResourceRelationship).where(
            ResourceRelationship.source_id == source.id,
            ResourceRelationship.target_id == target.id,
            ResourceRelationship.relationship_type == data.relationship_type,
        )
    )
    if existing is not None:
        if existing.source == "manual":
            raise ConflictError("This relationship already exists")
        # Confirming a discovered edge makes it permanent.
        existing.source = "manual"
        existing.confidence = 1.0
        rel = existing
    else:
        rel = ResourceRelationship(
            tenant_id=p.tenant_id,
            source_id=source.id,
            target_id=target.id,
            relationship_type=data.relationship_type,
            source="manual",
            confidence=1.0,
            properties={"description": data.description} if data.description else {},
        )
        s.add(rel)
    await s.flush()
    for r, other in ((source, target), (target, source)):
        s.add(
            _event(
                r,
                "relationship_added",
                f"{source.name} {data.relationship_type} {target.name}",
                p.username,
                {"relationship_id": str(rel.id), "other": str(other.id)},
            )
        )
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="resource.relationship_create",
        entity_type="resource",
        entity_id=source.id,
        details={
            "source": source.name,
            "target": target.name,
            "type": data.relationship_type,
        },
        client=ctx.client,
    )
    return rel


async def delete_relationship(ctx: RequestContext, relationship_id: uuid.UUID) -> None:
    s, p = ctx.session, ctx.principal
    rel = await s.get(ResourceRelationship, relationship_id)
    if rel is None:
        raise NotFoundError("Relationship not found")
    await s.delete(rel)
    await s.flush()
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="resource.relationship_delete",
        entity_type="resource",
        entity_id=rel.source_id,
        details={
            "source_id": str(rel.source_id),
            "target_id": str(rel.target_id),
            "type": rel.relationship_type,
            "origin": rel.source,
        },
        client=ctx.client,
    )


# Built only from the module constants above; no user input reaches this SQL.
_DEP_EDGES = f"""
    SELECT
      CASE WHEN relationship_type IN {INVERSE!r} THEN target_id ELSE source_id END AS dependent,
      CASE WHEN relationship_type IN {INVERSE!r} THEN source_id ELSE target_id END AS dependency
    FROM resource_relationships
    WHERE relationship_type IN {(*FORWARD, *INVERSE)!r}
"""  # noqa: S608

# Walk "dependents of" (downstream / blast radius) or "dependencies of" (upstream).
_WALK = """
WITH RECURSIVE dep AS ({edges}),
walk(rid, depth, path) AS (
    SELECT d.{near}, 1, ARRAY[CAST(:rid AS uuid), d.{near}]
    FROM dep d WHERE d.{far} = CAST(:rid AS uuid)
  UNION ALL
    SELECT d.{near}, w.depth + 1, w.path || d.{near}
    FROM dep d JOIN walk w ON d.{far} = w.rid
    WHERE w.depth < :depth AND NOT d.{near} = ANY(w.path)
)
SELECT rid, MIN(depth) AS depth FROM walk GROUP BY rid LIMIT 2000
"""


async def _walk(
    session: AsyncSession, resource_id: uuid.UUID, depth: int, downstream: bool
) -> dict[uuid.UUID, int]:
    near, far = ("dependent", "dependency") if downstream else ("dependency", "dependent")
    sql = _WALK.format(edges=_DEP_EDGES, near=near, far=far)
    rows = await session.execute(text(sql), {"rid": str(resource_id), "depth": depth})
    return {r[0]: int(r[1]) for r in rows.all() if r[0] != resource_id}


async def _graph_for(
    session: AsyncSession, node_depths: dict[uuid.UUID, tuple[int | None, str | None]]
) -> tuple[list[GraphNode], list[GraphEdge]]:
    ids = list(node_depths)
    if not ids:
        return [], []
    resources = await session.scalars(select(Resource).where(Resource.id.in_(ids)))
    nodes = [
        GraphNode(
            id=r.id,
            name=r.name,
            resource_type=r.resource_type,
            health_status=r.health_status,
            environment=r.environment,
            group_name=r.group_name,
            stale=r.stale,
            depth=node_depths[r.id][0],
            direction=node_depths[r.id][1],
        )
        for r in resources
    ]
    rels = await session.scalars(
        select(ResourceRelationship).where(
            ResourceRelationship.source_id.in_(ids), ResourceRelationship.target_id.in_(ids)
        )
    )
    edges = [
        GraphEdge(
            id=e.id,
            source_id=e.source_id,
            target_id=e.target_id,
            relationship_type=e.relationship_type,
            source=e.source,
            confidence=e.confidence,
        )
        for e in rels
    ]
    return nodes, edges


async def dependencies(
    session: AsyncSession, resource_id: uuid.UUID, direction: str, depth: int
) -> DependencyGraph:
    await get_resource(session, resource_id)
    up = await _walk(session, resource_id, depth, False) if direction in ("up", "both") else {}
    down = await _walk(session, resource_id, depth, True) if direction in ("down", "both") else {}
    node_depths: dict[uuid.UUID, tuple[int | None, str | None]] = {resource_id: (0, "self")}
    for rid, d in up.items():
        node_depths[rid] = (d, "upstream")
    for rid, d in down.items():
        node_depths.setdefault(rid, (d, "downstream"))
    nodes, edges = await _graph_for(session, node_depths)
    blast = sorted(down, key=lambda r: (down[r], str(r)))
    return DependencyGraph(nodes=nodes, edges=edges, blast_radius=blast)


async def blast_radius(
    session: AsyncSession, resource_id: uuid.UUID, depth: int = 6
) -> list[uuid.UUID]:
    return list(await _walk(session, resource_id, depth, True))


async def graph(
    session: AsyncSession,
    *,
    environment: str | None = None,
    group: str | None = None,
    resource_type: str | None = None,
    include_stale: bool = False,
) -> DependencyGraph:
    stmt = select(Resource.id)
    if environment:
        stmt = stmt.where(Resource.environment == environment)
    if group:
        stmt = stmt.where(Resource.group_name == group)
    if resource_type:
        stmt = stmt.where(Resource.resource_type == resource_type)
    if not include_stale:
        stmt = stmt.where(Resource.stale.is_(False))
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    ids = list(
        await session.scalars(
            stmt.order_by(Resource.resource_type, Resource.name).limit(GRAPH_LIMIT)
        )
    )
    nodes, edges = await _graph_for(session, {i: (None, None) for i in ids})
    return DependencyGraph(nodes=nodes, edges=edges, truncated=int(total or 0) > GRAPH_LIMIT)


async def environments_and_groups(session: AsyncSession) -> dict[str, list[str]]:
    envs = await session.scalars(
        select(Resource.environment).where(Resource.environment.is_not(None)).distinct()
    )
    groups = await session.scalars(
        select(Resource.group_name).where(Resource.group_name.is_not(None)).distinct()
    )
    tags = await session.scalars(select(func.jsonb_array_elements_text(Resource.tags)).distinct())
    return {
        "environments": sorted(e for e in envs if e),
        "groups": sorted(g for g in groups if g),
        "tags": sorted(t for t in tags if t)[:200],
    }
