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
from typing import Any

from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select

from app.agents.catalogue import AgentSpec, get_spec, merged_config
from app.core.config import Settings
from app.core.crypto import decrypt_secret
from app.database.session import tenant_session
from app.graphs import events
from app.llm.gateway import ChatMessage, EndpointConfig, ModelClient, ModelError, Usage
from app.models import Agent, AgentExecution, Investigation, ModelEndpoint


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
            )
        except ModelError:
            call.usage.calls += 1
            raise
        call.usage.add(result.usage)
        return result.value

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
