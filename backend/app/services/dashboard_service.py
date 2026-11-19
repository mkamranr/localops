"""Dashboard aggregates for the caller's tenant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select

from app.models import (
    ACTIVE_STATUSES,
    AgentExecution,
    Approval,
    AuditLog,
    Investigation,
    ModelEndpoint,
    Resource,
    User,
)
from app.schemas.audit import (
    ActivitySummary,
    CountSummary,
    DashboardSummary,
    IncidentSummary,
    ServiceHealth,
    UserSummary,
)
from app.services import incident_service
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

    inv = (
        await s.execute(
            select(
                func.count(case((Investigation.status.in_(ACTIVE_STATUSES), 1))),
                func.count(case((Investigation.started_at >= since, 1))),
            )
        )
    ).one()
    ex = (
        await s.execute(
            select(func.count(), func.count(case((AgentExecution.status == "failed", 1)))).where(
                AgentExecution.started_at >= since
            )
        )
    ).one()
    default_model = await s.scalar(
        select(ModelEndpoint.name).where(
            ModelEndpoint.is_default.is_(True), ModelEndpoint.enabled.is_(True)
        )
    )

    res = (
        await s.execute(
            select(
                func.count(),
                func.count().filter(Resource.health_status.in_(("healthy", "degraded"))),
                func.count().filter(Resource.health_status == "unhealthy"),
            ).where(Resource.stale.is_(False))
        )
    ).one()

    incidents = await incident_service.open_counts(s)
    pending_approvals = await s.scalar(
        select(func.count())
        .select_from(Approval)
        .where(Approval.status == "pending", Approval.expires_at > datetime.now(UTC))
    )

    return DashboardSummary(
        tenant=ctx.principal.tenant_slug,
        users=UserSummary(total=u[0], active=u[1], disabled=u[2]),
        activity=ActivitySummary(events_24h=a[0], logins_24h=a[1], failed_logins_24h=a[2]),
        services=services,
        resources=CountSummary(total=res[0], online=res[1], offline=res[2]),
        incidents=IncidentSummary(
            critical=incidents.get("critical", 0),
            high=incidents.get("high", 0),
            medium=incidents.get("medium", 0),
            low=incidents.get("low", 0) + incidents.get("informational", 0),
            open_total=sum(incidents.values()),
        ),
        active_investigations=inv[0],
        investigations_24h=inv[1],
        agent_executions_24h=ex[0],
        agent_failures_24h=ex[1],
        default_model=default_model,
        pending_approvals=int(pending_approvals or 0),
    )
