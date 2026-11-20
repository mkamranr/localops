"""Agent registry (spec §11): per-tenant agent configuration and execution history."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.catalogue import (
    CATALOGUE,
    CUSTOM_PREFIX,
    DEFAULT_AGENT_CONFIG,
    AgentSpec,
    custom_spec,
    get_spec,
    is_custom,
    merged_config,
)
from app.agents.diagnostics import TOOL_LOOP_AGENTS
from app.core.errors import AppError, ConflictError, NotFoundError, ValidationFailedError
from app.models import Agent, AgentExecution, ModelEndpoint
from app.schemas.ai import (
    AgentCreate,
    AgentExecutionOut,
    AgentOut,
    AgentSettings,
    AgentStats,
    AgentUpdate,
)
from app.schemas.common import PageParams
from app.services import audit_service as audit
from app.services.context import RequestContext
from app.tools import catalogue as tool_catalogue

# Settings that only mean something for agents running the tool loop.
TOOL_SETTINGS = ("allowed_tools", "max_steps", "instructions")


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
    out = {
        r[0]: AgentStats(
            executions_24h=r[1],
            failures_24h=r[2],
            avg_duration_ms=round(float(r[3]), 1) if r[3] is not None else None,
            tokens_24h=int(r[4]),
            last_run_at=r[5],
        )
        for r in rows
    }
    totals = await session.execute(
        select(
            AgentExecution.agent_type,
            func.count(),
            func.count(case((AgentExecution.status == "success", 1))),
            func.count(case((AgentExecution.status == "failed", 1))),
            func.max(AgentExecution.started_at),
            func.avg(AgentExecution.duration_ms),
        ).group_by(AgentExecution.agent_type)
    )
    for agent_type, total, ok, failed, last, avg in totals:
        stats = out.setdefault(agent_type, AgentStats())
        stats.executions_total = total
        stats.success_rate = round(ok / (ok + failed), 3) if ok + failed else None
        stats.last_run_at = last
        if stats.avg_duration_ms is None and avg is not None:
            stats.avg_duration_ms = round(float(avg), 1)
    return out


def _spec(agent: Agent) -> AgentSpec | None:
    if is_custom(agent.agent_type):
        return custom_spec(agent.agent_type, agent.name, agent.description)
    return get_spec(agent.agent_type)


def uses_tools(agent_type: str) -> bool:
    return agent_type in TOOL_LOOP_AGENTS or is_custom(agent_type)


def _to_out(agent: Agent, model_names: dict[uuid.UUID, str], stats: AgentStats) -> AgentOut:
    spec = _spec(agent)
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
        custom=is_custom(agent.agent_type),
        uses_tools=uses_tools(agent.agent_type),
        stats=stats,
        updated_at=agent.updated_at,
    )


async def _model_names(session: AsyncSession) -> dict[uuid.UUID, str]:
    return {r[0]: r[1] for r in await session.execute(select(ModelEndpoint.id, ModelEndpoint.name))}


_ORDER = {spec.agent_type: i for i, spec in enumerate(CATALOGUE)}


async def list_agents(ctx: RequestContext) -> list[AgentOut]:
    s = ctx.session
    agents = list(await s.scalars(select(Agent)))
    agents.sort(key=lambda a: (_ORDER.get(a.agent_type, 99), a.name.lower()))
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


async def _check_settings(
    session: AsyncSession, agent_type: str, settings: AgentSettings
) -> dict[str, Any]:
    """Validate settings against the agent and the tenant's tool catalogue.

    Only read tools can be allowed: change tools never reach an agent, whatever the
    configuration says (the catalogue also filters them out at run time).
    """
    values = settings.model_dump(exclude_none=True)
    if not uses_tools(agent_type):
        given = [k for k in TOOL_SETTINGS if k in values and values[k] not in ([], "")]
        if given:
            raise ValidationFailedError(
                f"{', '.join(given)} only apply to agents that call tools "
                "(diagnostics and custom agents)"
            )
    names = values.get("allowed_tools") or []
    if names:
        catalogue = await tool_catalogue.load(session)
        unknown = [n for n in names if catalogue.get(n) is None]
        if unknown:
            raise ValidationFailedError(
                f"Unknown or disabled tool(s): {', '.join(unknown)}",
                details={"tools": unknown},
            )
        change = [n for n in names if catalogue.tools[n].is_change]
        if change:
            raise ValidationFailedError(
                f"Remediation (change) tools cannot be given to agents: {', '.join(change)}; "
                "they run only through the approval workflow",
                details={"tools": change},
            )
    return values


async def _check_model(session: AsyncSession, model_id: uuid.UUID) -> None:
    endpoint = await session.get(ModelEndpoint, model_id)
    if endpoint is None:
        raise AppError("Model not found", code="invalid_model", status_code=422)
    if endpoint.purpose != "chat":
        raise AppError(
            "Agents need a chat model, not an embedding model",
            code="invalid_model",
            status_code=422,
        )


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    slug = slug.removesuffix("_agent").removeprefix("custom_")
    if not slug or not slug[0].isalpha():
        slug = f"agent_{slug}".rstrip("_")
    return slug[:40].rstrip("_")


async def create_agent(ctx: RequestContext, data: AgentCreate) -> AgentOut:
    """Create a custom tool-loop specialist (agent_type ``custom_<slug>``)."""
    s, p = ctx.session, ctx.principal
    slug = data.slug or _slugify(data.name)
    if slug.endswith("_agent"):
        raise ValidationFailedError("The slug must not end with '_agent'")
    agent_type = f"{CUSTOM_PREFIX}{slug}"
    if await s.scalar(select(Agent.id).where(Agent.agent_type == agent_type)):
        raise ConflictError(f"An agent named {agent_type} already exists")
    settings = await _check_settings(s, agent_type, data)
    if data.model_id is not None:
        await _check_model(s, data.model_id)
    config = {
        **DEFAULT_AGENT_CONFIG,
        **{k: v for k, v in settings.items() if k in DEFAULT_AGENT_CONFIG},
    }
    agent = Agent(
        tenant_id=p.tenant_id,
        agent_type=agent_type,
        name=data.name.strip(),
        description=data.description.strip(),
        model_id=data.model_id,
        configuration=config,
        enabled=data.enabled,
    )
    s.add(agent)
    await s.flush()
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="agent.create",
        entity_type="agent",
        entity_id=agent.id,
        details={
            "agent_type": agent_type,
            "name": agent.name,
            "allowed_tools": config["allowed_tools"],
            "model_id": str(data.model_id) if data.model_id else None,
        },
        client=ctx.client,
    )
    return await get_agent(ctx, agent.id)


async def delete_agent(ctx: RequestContext, agent_id: uuid.UUID) -> None:
    s, p = ctx.session, ctx.principal
    agent = await _get(s, agent_id)
    if not is_custom(agent.agent_type):
        raise ConflictError("Built-in agents cannot be deleted; disable them instead")
    details = {"agent_type": agent.agent_type, "name": agent.name}
    await s.delete(agent)
    await s.flush()
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="agent.delete",
        entity_type="agent",
        entity_id=agent_id,
        details=details,
        client=ctx.client,
    )


async def update_agent(ctx: RequestContext, agent_id: uuid.UUID, data: AgentUpdate) -> AgentOut:
    s, p = ctx.session, ctx.principal
    agent = await _get(s, agent_id)
    changes: dict[str, Any] = {}
    if data.name is not None and data.name != agent.name:
        agent.name = changes["name"] = data.name.strip()
    if data.description is not None and data.description != agent.description:
        agent.description = changes["description"] = data.description
    if "model_id" in data.model_fields_set and data.model_id != agent.model_id:
        if data.model_id is not None:
            await _check_model(s, data.model_id)
        agent.model_id = data.model_id
        changes["model_id"] = str(data.model_id) if data.model_id else None
    if data.configuration is not None:
        values = await _check_settings(s, agent.agent_type, data.configuration)
        if is_custom(agent.agent_type) and values.get("allowed_tools") == []:
            raise ValidationFailedError("A custom agent needs at least one allowed tool")
        new = {**agent.configuration, **values}
        if new != agent.configuration:
            agent.configuration = new
            changes["configuration"] = values
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
    status: str | None = None,
    errors_only: bool = False,
) -> tuple[list[AgentExecutionOut], int]:
    q = select(AgentExecution)
    if agent_type:
        q = q.where(AgentExecution.agent_type == agent_type)
    if status:
        q = q.where(AgentExecution.status == status)
    if errors_only:
        q = q.where(AgentExecution.error.is_not(None))
    if investigation_id:
        q = q.where(AgentExecution.investigation_id == investigation_id)
    total = await ctx.session.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = await ctx.session.scalars(
        q.order_by(AgentExecution.started_at.desc()).offset(page.offset).limit(page.page_size)
    )
    return [AgentExecutionOut.model_validate(r) for r in rows], total
