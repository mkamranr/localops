"""Dashboard aggregates for the caller's tenant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select

from app.models import AuditLog, User
from app.schemas.audit import (
    ActivitySummary,
    CountSummary,
    DashboardSummary,
    IncidentSummary,
    ServiceHealth,
    UserSummary,
)
from app.services.context import RequestContext


async def summary(ctx: RequestContext, services: list[ServiceHealth]) -> DashboardSummary:
    s, tid = ctx.session, ctx.principal.tenant_id

    u = (
        await s.execute(
            select(
                func.count(),
                func.count(case((User.status == "active", 1))),
                func.count(case((User.status == "disabled", 1))),
            ).where(User.tenant_id == tid, User.status != "deleted")
        )
    ).one()

    since = datetime.now(UTC) - timedelta(hours=24)
    a = (
        await s.execute(
            select(
                func.count(),
                func.count(
                    case(((AuditLog.action == "auth.login") & (AuditLog.outcome == "success"), 1))
                ),
                func.count(
                    case(((AuditLog.action == "auth.login") & (AuditLog.outcome == "failure"), 1))
                ),
            ).where(AuditLog.tenant_id == tid, AuditLog.created_at >= since)
        )
    ).one()

    return DashboardSummary(
        tenant=ctx.principal.tenant_slug,
        users=UserSummary(total=u[0], active=u[1], disabled=u[2]),
        activity=ActivitySummary(events_24h=a[0], logins_24h=a[1], failed_logins_24h=a[2]),
        services=services,
        resources=CountSummary(),
        incidents=IncidentSummary(),
    )
