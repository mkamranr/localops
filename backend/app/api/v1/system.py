"""Audit log, dashboard and system health endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.api.dependencies import redis_dep, require_permission, settings_dep
from app.core.config import Settings
from app.schemas.audit import AuditLogOut, DashboardSummary, ReadinessResponse
from app.schemas.common import Page, PageParams
from app.services import audit_service, dashboard_service, health_service
from app.services.context import RequestContext

router = APIRouter()


@router.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    """Liveness: the process is up and serving requests."""
    return {"status": "ok"}


@router.get("/ready", tags=["system"], response_model=ReadinessResponse)
async def ready(
    settings: Settings = Depends(settings_dep), redis: Redis = Depends(redis_dep)
) -> JSONResponse:
    """Readiness: PostgreSQL, Redis and Qdrant are reachable."""
    services = await health_service.check_all(settings, redis)
    ok = all(s.status == "ok" for s in services)
    body = ReadinessResponse(status="ready" if ok else "degraded", services=services)
    return JSONResponse(body.model_dump(), status_code=200 if ok else 503)


@router.get("/audit-logs", tags=["audit"], response_model=Page[AuditLogOut])
async def list_audit_logs(
    page: PageParams = Depends(),
    action: str | None = Query(None, max_length=100),
    user_id: uuid.UUID | None = None,
    entity_type: str | None = Query(None, max_length=50),
    entity_id: uuid.UUID | None = None,
    outcome: Literal["success", "failure"] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    ctx: RequestContext = Depends(require_permission("audit.read")),
) -> Page[AuditLogOut]:
    rows, total = await audit_service.list_logs(
        ctx.session,
        page,
        action=action,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        outcome=outcome,
        date_from=date_from,
        date_to=date_to,
    )
    return Page(
        items=[AuditLogOut.model_validate(r) for r in rows],
        total=total,
        page=page.page,
        page_size=page.page_size,
    )


@router.get("/dashboard/summary", tags=["dashboard"], response_model=DashboardSummary)
async def dashboard_summary(
    ctx: RequestContext = Depends(require_permission("dashboard.read")),
    settings: Settings = Depends(settings_dep),
    redis: Redis = Depends(redis_dep),
) -> DashboardSummary:
    services = await health_service.check_all(settings, redis)
    return await dashboard_service.summary(ctx, services)


@router.get("/dashboard/activity", tags=["dashboard"], response_model=list[AuditLogOut])
async def dashboard_activity(
    limit: int = Query(10, ge=1, le=50),
    ctx: RequestContext = Depends(require_permission("dashboard.read", "audit.read")),
) -> list[AuditLogOut]:
    rows, _ = await audit_service.list_logs(ctx.session, PageParams(page=1, page_size=limit))
    return [AuditLogOut.model_validate(r) for r in rows]
