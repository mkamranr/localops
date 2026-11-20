"""Model registry and agent registry endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response

from app.api.dependencies import (
    client_info,
    require_permission,
    require_principal,
    settings_dep,
)
from app.core.config import Settings
from app.schemas.ai import (
    AgentCreate,
    AgentExecutionOut,
    AgentOut,
    AgentUpdate,
    ModelEndpointCreate,
    ModelEndpointOut,
    ModelEndpointUpdate,
    ModelTestResult,
)
from app.schemas.common import Page, PageParams
from app.services import agent_service, model_service
from app.services.context import ClientInfo, Principal, RequestContext

router = APIRouter(tags=["ai"])

# ------------------------------------------------------------------ models


@router.get("/models", response_model=list[ModelEndpointOut])
async def list_models(
    ctx: RequestContext = Depends(require_permission("settings.read")),
) -> list[ModelEndpointOut]:
    return [model_service.to_out(m) for m in await model_service.list_models(ctx.session)]


@router.post("/models", response_model=ModelEndpointOut, status_code=201)
async def create_model(
    body: ModelEndpointCreate,
    settings: Settings = Depends(settings_dep),
    ctx: RequestContext = Depends(require_permission("settings.manage")),
) -> ModelEndpointOut:
    return model_service.to_out(await model_service.create_model(ctx, settings, body))


@router.get("/models/{model_id}", response_model=ModelEndpointOut)
async def get_model(
    model_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("settings.read")),
) -> ModelEndpointOut:
    return model_service.to_out(await model_service.get_model(ctx.session, model_id))


@router.put("/models/{model_id}", response_model=ModelEndpointOut)
async def update_model(
    model_id: uuid.UUID,
    body: ModelEndpointUpdate,
    settings: Settings = Depends(settings_dep),
    ctx: RequestContext = Depends(require_permission("settings.manage")),
) -> ModelEndpointOut:
    return model_service.to_out(await model_service.update_model(ctx, settings, model_id, body))


@router.delete("/models/{model_id}", status_code=204)
async def delete_model(
    model_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("settings.manage")),
) -> Response:
    await model_service.delete_model(ctx, model_id)
    return Response(status_code=204)


@router.post("/models/{model_id}/test", response_model=ModelTestResult)
async def test_model(
    model_id: uuid.UUID,
    settings: Settings = Depends(settings_dep),
    client: ClientInfo = Depends(client_info),
    principal: Principal = Depends(require_principal("settings.manage")),
) -> ModelTestResult:
    return await model_service.test_model(principal, client, settings, model_id)


# ------------------------------------------------------------------ agents


@router.get("/agents", response_model=list[AgentOut])
async def list_agents(
    ctx: RequestContext = Depends(require_permission("agent.read")),
) -> list[AgentOut]:
    return await agent_service.list_agents(ctx)


@router.get("/agents/executions", response_model=Page[AgentExecutionOut])
async def list_executions(
    page: PageParams = Depends(),
    agent_type: str | None = Query(None, max_length=50),
    status: str | None = Query(None, pattern="^(running|success|failed)$"),
    errors_only: bool = False,
    investigation_id: uuid.UUID | None = None,
    ctx: RequestContext = Depends(require_permission("agent.read")),
) -> Page[AgentExecutionOut]:
    rows, total = await agent_service.list_executions(
        ctx,
        page,
        agent_type=agent_type,
        investigation_id=investigation_id,
        status=status,
        errors_only=errors_only,
    )
    return Page(items=rows, total=total, page=page.page, page_size=page.page_size)


@router.post("/agents", response_model=AgentOut, status_code=201)
async def create_agent(
    body: AgentCreate,
    ctx: RequestContext = Depends(require_permission("agent.configure")),
) -> AgentOut:
    """Create a custom specialist: a tool-loop agent limited to the given read tools."""
    return await agent_service.create_agent(ctx, body)


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("agent.configure")),
) -> Response:
    await agent_service.delete_agent(ctx, agent_id)
    return Response(status_code=204)


@router.get("/agents/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("agent.read")),
) -> AgentOut:
    return await agent_service.get_agent(ctx, agent_id)


@router.put("/agents/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: uuid.UUID,
    body: AgentUpdate,
    ctx: RequestContext = Depends(require_permission("agent.configure")),
) -> AgentOut:
    return await agent_service.update_agent(ctx, agent_id, body)


@router.post("/agents/{agent_id}/enable", response_model=AgentOut)
async def enable_agent(
    agent_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("agent.configure")),
) -> AgentOut:
    return await agent_service.update_agent(ctx, agent_id, AgentUpdate(enabled=True))


@router.post("/agents/{agent_id}/disable", response_model=AgentOut)
async def disable_agent(
    agent_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("agent.configure")),
) -> AgentOut:
    return await agent_service.update_agent(ctx, agent_id, AgentUpdate(enabled=False))
