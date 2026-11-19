"""Per-run dependencies for investigation graphs.

Graph nodes never touch infrastructure directly: they get an ``InvestigationRuntime``
that provides tenant-bound persistence, event emission, cancellation checks and
model access. All DB access uses short transactions so no connection is held while a
model call is in flight.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.catalogue import AgentSpec, get_spec, merged_config
from app.core.config import Settings
from app.core.crypto import decrypt_secret
from app.database.session import tenant_session
from app.graphs import events
from app.llm.gateway import ChatMessage, EndpointConfig, ModelClient, ModelError, Usage
from app.models import Agent, AgentExecution, Investigation, ModelEndpoint, ToolExecution

if TYPE_CHECKING:  # the tool stack imports services that import the dispatcher
    from app.agents.toolloop import ToolBudget
    from app.llm.tool_calling import ToolExchange, ToolTurn
    from app.tools.base import ToolSpec
    from app.tools.executor import ToolRunResult


class InvestigationCancelledError(Exception):
    pass


class AgentUnavailableError(Exception):
    """The agent is disabled or has no usable model."""


@dataclass(slots=True)
class AgentBinding:
    spec: AgentSpec
    enabled: bool
    config: dict[str, Any]
    endpoint: EndpointConfig | None
    endpoint_id: uuid.UUID | None


@dataclass(slots=True)
class NodeCall:
    """Accumulates model usage for one instrumented node execution."""

    usage: Usage = field(default_factory=Usage)
    model_name: str | None = None
    endpoint_id: uuid.UUID | None = None


ClientFactory = Callable[[EndpointConfig], ModelClient]


@dataclass(slots=True)
class InvestigationRuntime:
    settings: Settings
    tenant_id: uuid.UUID
    investigation_id: uuid.UUID
    redis: Redis | None = None
    client_factory: ClientFactory = ModelClient
    on_event: Callable[[str], Awaitable[None]] | None = None
    _budget: ToolBudget | None = None

    # ------------------------------------------------------------------ events

    async def emit(
        self, event_type: str, agent: str | None = None, data: dict[str, Any] | None = None
    ) -> int:
        async with tenant_session(self.tenant_id) as session:
            seq = await events.append_event(
                session,
                tenant_id=self.tenant_id,
                investigation_id=self.investigation_id,
                event_type=event_type,
                agent=agent,
                data=data,
            )
        await events.notify(self.redis, self.tenant_id, self.investigation_id)
        if self.on_event:
            await self.on_event(event_type)
        return seq

    # ------------------------------------------------------------ cancellation

    async def check_cancelled(self) -> None:
        async with tenant_session(self.tenant_id) as session:
            cancelled = await session.scalar(
                select(Investigation.cancel_requested).where(
                    Investigation.id == self.investigation_id
                )
            )
        if cancelled:
            raise InvestigationCancelledError

    # ------------------------------------------------------------ inventory

    async def resolve_entities(self, entities: list[str]) -> list[dict[str, Any]]:
        from app.agents import discovery

        async with tenant_session(self.tenant_id) as session:
            return await discovery.resolve_entities(session, entities)

    async def inventory_evidence(self, resource_ids: list[uuid.UUID]) -> list[dict[str, Any]]:
        from app.agents import discovery

        async with tenant_session(self.tenant_id) as session:
            return await discovery.evidence_for(session, resource_ids)

    async def set_resource_ids(self, resource_ids: list[str]) -> None:
        async with tenant_session(self.tenant_id) as session:
            inv = await session.get(Investigation, self.investigation_id)
            if inv is not None:
                inv.resource_ids = list(dict.fromkeys(resource_ids))

    # --------------------------------------------------------------- agents

    async def bind_agent(self, agent_type: str) -> AgentBinding:
        spec = get_spec(agent_type)
        if spec is None:
            raise AgentUnavailableError(f"Unknown agent '{agent_type}'")
        async with tenant_session(self.tenant_id) as session:
            agent = await session.scalar(select(Agent).where(Agent.agent_type == agent_type))
            enabled = agent.enabled if agent else True
            config = merged_config(agent.configuration if agent else None)
            endpoint: ModelEndpoint | None = None
            if agent and agent.model_id:
                endpoint = await session.get(ModelEndpoint, agent.model_id)
                if endpoint is not None and not endpoint.enabled:
                    endpoint = None
            if endpoint is None:
                endpoint = await session.scalar(
                    select(ModelEndpoint).where(
                        ModelEndpoint.is_default.is_(True), ModelEndpoint.enabled.is_(True)
                    )
                )
            ep_config: EndpointConfig | None = None
            ep_id: uuid.UUID | None = None
            if endpoint is not None:
                api_key = (
                    decrypt_secret(self.settings, endpoint.encrypted_api_key)
                    if endpoint.encrypted_api_key
                    else None
                )
                ep_config = EndpointConfig.from_record(
                    provider=endpoint.provider,
                    base_url=endpoint.base_url,
                    model_name=endpoint.model_name,
                    name=endpoint.name,
                    configuration={
                        **endpoint.configuration,
                        # agent-level limits take precedence over endpoint defaults
                        "timeout_seconds": config.get(
                            "timeout_seconds", endpoint.configuration.get("timeout_seconds", 120)
                        ),
                        "max_retries": config.get(
                            "max_retries", endpoint.configuration.get("max_retries", 2)
                        ),
                    },
                    api_key=api_key,
                )
                ep_id = endpoint.id
        return AgentBinding(spec, enabled, config, ep_config, ep_id)

    async def structured[T: BaseModel](
        self,
        call: NodeCall,
        binding: AgentBinding,
        messages: list[ChatMessage],
        schema: type[T],
        *,
        example: dict[str, Any] | None = None,
    ) -> T:
        if binding.endpoint is None:
            raise AgentUnavailableError(
                "No AI model is configured. Add one under Settings → AI Models and mark it "
                "as default, or assign a model to the agent."
            )
        client = self.client_factory(binding.endpoint)
        call.model_name = binding.endpoint.model
        call.endpoint_id = binding.endpoint_id
        try:
            result = await client.structured(
                messages,
                schema,
                temperature=float(binding.config["temperature"]),
                max_tokens=int(binding.config["max_tokens"]),
                example=example,
            )
        except ModelError:
            call.usage.calls += 1
            raise
        call.usage.add(result.usage)
        return result.value

    # ----------------------------------------------------------------- tools

    async def chat_tools(
        self,
        call: NodeCall,
        binding: AgentBinding,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]],
        history: list[ToolExchange],
    ) -> ToolTurn:
        if binding.endpoint is None:
            raise AgentUnavailableError(
                "No AI model is configured. Add one under Settings → AI Models and mark it "
                "as default, or assign a model to the agent."
            )
        from app.llm.tool_calling import chat_tools

        client = self.client_factory(binding.endpoint)
        call.model_name = binding.endpoint.model
        call.endpoint_id = binding.endpoint_id
        try:
            turn = await chat_tools(
                client,
                messages,
                tools,
                history,
                temperature=float(binding.config["temperature"]),
                max_tokens=int(binding.config["max_tokens"]),
            )
        except ModelError:
            call.usage.calls += 1
            raise
        call.usage.add(turn.usage)
        return turn

    async def tool_budget(self) -> ToolBudget:
        """The investigation's remaining tool calls, shared by all its agents.

        Counted from recorded executions, so a resumed run does not get a fresh budget.
        """
        from app.agents.toolloop import ToolBudget

        if self._budget is None:
            async with tenant_session(self.tenant_id) as session:
                used = await session.scalar(
                    select(func.count())
                    .select_from(ToolExecution)
                    .where(
                        ToolExecution.investigation_id == self.investigation_id,
                        ToolExecution.status != "rejected",
                    )
                )
            self._budget = ToolBudget(
                max(self.settings.investigation_max_tool_calls - int(used or 0), 0)
            )
        return self._budget

    async def run_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        evidence_id: str,
        *,
        agent_type: str,
        allowed: list[ToolSpec],
        allow_remote: bool,
    ) -> ToolRunResult:
        from app.tools.executor import run_tool

        return await run_tool(
            self.settings,
            self.tenant_id,
            name,
            arguments,
            allow_remote=allow_remote,
            allowed=allowed,
            investigation_id=self.investigation_id,
            agent_type=agent_type,
            actor=f"agent:{agent_type}",
            evidence_id=evidence_id,
        )

    async def in_session[T](self, fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
        """Run ``fn`` in a short tenant-bound transaction."""
        async with tenant_session(self.tenant_id) as session:
            return await fn(session)

    async def resources_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        from app.agents import discovery

        async with tenant_session(self.tenant_id) as session:
            return await discovery.resolve_ids(session, ids)

    # ------------------------------------------------------------ executions

    async def start_execution(self, agent_type: str, node: str) -> tuple[uuid.UUID, float]:
        async with tenant_session(self.tenant_id) as session:
            row = AgentExecution(
                tenant_id=self.tenant_id,
                investigation_id=self.investigation_id,
                agent_type=agent_type,
                node=node,
                status="running",
                started_at=datetime.now(UTC),
            )
            session.add(row)
            await session.flush()
            return row.id, time.perf_counter()

    async def finish_execution(
        self,
        execution: tuple[uuid.UUID, float],
        *,
        status: str,
        call: NodeCall,
        error: str | None = None,
    ) -> int:
        exec_id, started = execution
        duration = int((time.perf_counter() - started) * 1000)
        async with tenant_session(self.tenant_id) as session:
            row = await session.get(AgentExecution, exec_id)
            if row is not None:
                row.status = status
                row.completed_at = datetime.now(UTC)
                row.duration_ms = duration
                row.model_name = call.model_name
                row.model_endpoint_id = call.endpoint_id
                row.prompt_tokens = call.usage.prompt_tokens or None
                row.completion_tokens = call.usage.completion_tokens or None
                row.llm_calls = call.usage.calls
                row.error = error[:1000] if error else None
        return duration
