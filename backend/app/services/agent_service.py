"""Agent registry (spec §11): per-tenant agent configuration and execution history."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.catalogue import CATALOGUE, DEFAULT_AGENT_CONFIG, get_spec, merged_config
from app.core.errors import AppError, NotFoundError
from app.models import Agent, AgentExecution, ModelEndpoint
from app.schemas.ai import AgentExecutionOut, AgentOut, AgentStats, AgentUpdate
from app.schemas.common import PageParams
from app.services import audit_service as audit
from app.services.context import RequestContext


async def ensure_agents(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Insert catalogue agents missing for the tenant (session must be in its context)."""
    existing = set(await session.scalars(select(Agent.agent_type)))
    added = 0
    for spec in CATALOGUE:
        if spec.agent_type in existing:
            continue
        session.add(
            Agent(
                tenant_id=tenant_id,
                agent_type=spec.agent_type,
                name=spec.name,
                description=spec.description,
                configuration=dict(DEFAULT_AGENT_CONFIG),
                enabled=True,
            )
        )
        added += 1
    if added:
        await session.flush()
    return added


async def _stats(session: AsyncSession) -> dict[str, AgentStats]:
    since = datetime.now(UTC) - timedelta(hours=24)
    rows = await session.execute(
        select(
            AgentExecution.agent_type,
            func.count(),
            func.count(case((AgentExecution.status == "failed", 1))),
            func.avg(AgentExecution.duration_ms),
            func.coalesce(
                func.sum(
                    func.coalesce(AgentExecution.prompt_tokens, 0)
                    + func.coalesce(AgentExecution.completion_tokens, 0)
                ),
                0,
            ),
            func.max(AgentExecution.started_at),
        )
        .where(AgentExecution.started_at >= since)
        .group_by(AgentExecution.agent_type)
    )
    return {
        r[0]: AgentStats(
            executions_24h=r[1],
            failures_24h=r[2],
            avg_duration_ms=round(float(r[3]), 1) if r[3] is not None else None,
            tokens_24h=int(r[4]),
            last_run_at=r[5],
        )
        for r in rows
    }


def _to_out(agent: Agent, model_names: dict[uuid.UUID, str], stats: AgentStats) -> AgentOut:
    spec = get_spec(agent.agent_type)
    return AgentOut(
        id=agent.id,
        agent_type=agent.agent_type,
        name=agent.name,
        description=agent.description,
        enabled=agent.enabled,
        model_id=agent.model_id,
        model_name=model_names.get(agent.model_id) if agent.model_id else None,
        configuration=merged_config(agent.configuration),
        capabilities=list(spec.capabilities) if spec else [],
        available_in_phase=spec.available_in_phase if spec else 99,
        implemented=spec.implemented if spec else False,
        specialist=spec.specialist if spec else True,
        stats=stats,
        updated_at=agent.updated_at,
    )


async def _model_names(session: AsyncSession) -> dict[uuid.UUID, str]:
    return {r[0]: r[1] for r in await session.execute(select(ModelEndpoint.id, ModelEndpoint.name))}


_ORDER = {spec.agent_type: i for i, spec in enumerate(CATALOGUE)}


async def list_agents(ctx: RequestContext) -> list[AgentOut]:
    s = ctx.session
    agents = list(await s.scalars(select(Agent)))
    agents.sort(key=lambda a: _ORDER.get(a.agent_type, 99))
    names, stats = await _model_names(s), await _stats(s)
    return [_to_out(a, names, stats.get(a.agent_type, AgentStats())) for a in agents]


async def _get(session: AsyncSession, agent_id: uuid.UUID) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise NotFoundError("Agent not found")
    return agent


async def get_agent(ctx: RequestContext, agent_id: uuid.UUID) -> AgentOut:
    agent = await _get(ctx.session, agent_id)
    names, stats = await _model_names(ctx.session), await _stats(ctx.session)
    return _to_out(agent, names, stats.get(agent.agent_type, AgentStats()))


async def update_agent(ctx: RequestContext, agent_id: uuid.UUID, data: AgentUpdate) -> AgentOut:
    s, p = ctx.session, ctx.principal
    agent = await _get(s, agent_id)
    changes: dict[str, Any] = {}
    if data.name is not None and data.name != agent.name:
        agent.name = changes["name"] = data.name.strip()
    if data.description is not None and data.description != agent.description:
        agent.description = changes["description"] = data.description
    if "model_id" in data.model_fields_set and data.model_id != agent.model_id:
        if data.model_id is not None and await s.get(ModelEndpoint, data.model_id) is None:
            raise AppError("Model not found", code="invalid_model", status_code=422)
        agent.model_id = data.model_id
        changes["model_id"] = str(data.model_id) if data.model_id else None
    if data.configuration is not None:
        new = {**agent.configuration, **data.configuration.model_dump(exclude_none=True)}
        if new != agent.configuration:
            agent.configuration = new
            changes["configuration"] = data.configuration.model_dump(exclude_none=True)
    if data.enabled is not None and data.enabled != agent.enabled:
        if not data.enabled and agent.agent_type == "supervisor":
            # Allowed, but investigations will refuse to start; make that explicit.
            changes["warning"] = "supervisor disabled; investigations cannot run"
        agent.enabled = changes["enabled"] = data.enabled
    await s.flush()
    if changes:
        await audit.record(
            s,
            tenant_id=p.tenant_id,
            actor=p,
            action="agent.update",
            entity_type="agent",
            entity_id=agent.id,
            details={"agent_type": agent.agent_type, "changes": changes},
            client=ctx.client,
        )
    return await get_agent(ctx, agent.id)


async def list_executions(
    ctx: RequestContext,
    page: PageParams,
    *,
    agent_type: str | None = None,
    investigation_id: uuid.UUID | None = None,
) -> tuple[list[AgentExecutionOut], int]:
    q = select(AgentExecution)
    if agent_type:
        q = q.where(AgentExecution.agent_type == agent_type)
    if investigation_id:
        q = q.where(AgentExecution.investigation_id == investigation_id)
    total = await ctx.session.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = await ctx.session.scalars(
        q.order_by(AgentExecution.started_at.desc()).offset(page.offset).limit(page.page_size)
    )
    return [AgentExecutionOut.model_validate(r) for r in rows], total
