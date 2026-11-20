"""The read-only diagnostic tool catalogue, and running a tool on demand."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select

from app.api.dependencies import client_info, require_permission, require_principal, settings_dep
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.models import ToolExecution
from app.schemas.incidents import ToolExecutionOut, ToolOut, ToolRunOut, ToolRunRequest
from app.schemas.tooling import (
    HttpToolCreate,
    HttpToolUpdate,
    ToolDefinitionOut,
    ToolDefinitionUpdate,
    ToolSyncResult,
    ToolTestRequest,
)
from app.services import tool_service
from app.services.context import ClientInfo, Principal, RequestContext
from app.tools import catalogue as tool_catalogue
from app.tools.executor import run_tool

router = APIRouter(tags=["tools"])


@router.get("/tools", response_model=list[ToolOut])
async def list_tools(
    resource_type: str | None = Query(None, max_length=30),
    ctx: RequestContext = Depends(require_permission("incident.read")),
) -> list[ToolOut]:
    """Built-in tools plus this tenant's enabled MCP / HTTP tools."""
    catalogue = await tool_catalogue.load(ctx.session)
    specs = [
        t
        for t in catalogue.tools.values()
        if resource_type is None or resource_type in t.resource_types
    ]
    return [ToolOut.model_validate(t.describe()) for t in specs]


@router.post("/tools/{name}/run", response_model=ToolRunOut)
async def run_tool_now(
    name: str,
    body: ToolRunRequest,
    settings: Settings = Depends(settings_dep),
    principal: Principal = Depends(require_principal("tool.execute.readonly")),
) -> ToolRunOut:
    """Run one read-only tool (validated, permission-checked, redacted, audited)."""
    catalogue = await tool_catalogue.for_tenant(principal.tenant_id)
    spec = catalogue.get(name)
    if spec is None:
        raise NotFoundError(f"Unknown tool '{name}'")
    arguments = dict(body.arguments)
    if body.resource_id is not None:
        arguments["resource_id"] = str(body.resource_id)
    result = await run_tool(
        settings,
        principal.tenant_id,
        name,
        arguments,
        allow_remote=True,
        actor=principal.username,
        user_id=principal.user_id,
        catalogue=catalogue,
        permissions=set(principal.permissions),
    )
    return ToolRunOut(
        tool=result.tool,
        status=result.status,
        summary=result.summary,
        output=result.model_text,
        error=result.error,
        duration_ms=result.duration_ms,
        execution_id=result.execution_id,
        resource_name=result.resource_name,
    )


@router.get(
    "/investigations/{investigation_id}/tool-executions", response_model=list[ToolExecutionOut]
)
async def investigation_tool_executions(
    investigation_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("incident.read")),
) -> list[ToolExecutionOut]:
    rows = await ctx.session.scalars(
        select(ToolExecution)
        .where(ToolExecution.investigation_id == investigation_id)
        .order_by(ToolExecution.created_at)
        .limit(500)
    )
    return [ToolExecutionOut.model_validate(r) for r in rows]


@router.get("/resources/{resource_id}/tool-executions", response_model=list[ToolExecutionOut])
async def resource_tool_executions(
    resource_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    ctx: RequestContext = Depends(require_permission("resource.read")),
) -> list[ToolExecutionOut]:
    rows = await ctx.session.scalars(
        select(ToolExecution)
        .where(ToolExecution.resource_id == resource_id)
        .order_by(ToolExecution.created_at.desc())
        .limit(limit)
    )
    if rows is None:  # pragma: no cover
        raise NotFoundError("Resource not found")
    return [ToolExecutionOut.model_validate(r) for r in rows]


# ------------------------------------------------------------------ registered tools


@router.get("/tools/definitions", response_model=list[ToolDefinitionOut])
async def list_definitions(
    integration_id: uuid.UUID | None = None,
    source: str | None = Query(None, pattern="^(mcp|http)$"),
    status: str | None = Query(None, pattern="^(new|active|schema_changed|missing)$"),
    ctx: RequestContext = Depends(require_permission("integration.read")),
) -> list[ToolDefinitionOut]:
    return await tool_service.list_definitions(
        ctx.session, integration_id=integration_id, source=source, status=status
    )


@router.post("/tools/definitions", response_model=ToolDefinitionOut, status_code=201)
async def create_http_tool(
    body: HttpToolCreate,
    ctx: RequestContext = Depends(require_permission("tool.manage")),
) -> ToolDefinitionOut:
    """Create a custom HTTP tool on an ``http_api`` integration."""
    return await tool_service.create_http_tool(ctx, body)


@router.get("/tools/definitions/{definition_id}", response_model=ToolDefinitionOut)
async def get_definition(
    definition_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("integration.read")),
) -> ToolDefinitionOut:
    return await tool_service.get_definition(ctx.session, definition_id)


@router.put("/tools/definitions/{definition_id}", response_model=ToolDefinitionOut)
async def update_definition(
    definition_id: uuid.UUID,
    body: ToolDefinitionUpdate,
    ctx: RequestContext = Depends(require_permission("tool.manage")),
) -> ToolDefinitionOut:
    """Review, enable/disable, classify (read/change), approve a changed schema."""
    return await tool_service.update_definition(ctx, definition_id, body)


@router.put("/tools/definitions/{definition_id}/http", response_model=ToolDefinitionOut)
async def update_http_tool(
    definition_id: uuid.UUID,
    body: HttpToolUpdate,
    ctx: RequestContext = Depends(require_permission("tool.manage")),
) -> ToolDefinitionOut:
    return await tool_service.update_http_tool(ctx, definition_id, body)


@router.delete("/tools/definitions/{definition_id}", status_code=204)
async def delete_definition(
    definition_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("tool.manage")),
) -> Response:
    await tool_service.delete_definition(ctx, definition_id)
    return Response(status_code=204)


@router.post("/tools/definitions/{definition_id}/test", response_model=ToolRunOut)
async def test_definition(
    definition_id: uuid.UUID,
    body: ToolTestRequest,
    settings: Settings = Depends(settings_dep),
    principal: Principal = Depends(require_principal("tool.manage")),
) -> ToolRunOut:
    result = await tool_service.test_definition(principal, settings, definition_id, body.arguments)
    return ToolRunOut(
        tool=result.tool,
        status=result.status,
        summary=result.summary,
        output=result.model_text,
        error=result.error,
        duration_ms=result.duration_ms,
        execution_id=result.execution_id,
        resource_name=result.resource_name,
    )


@router.post("/integrations/{integration_id}/sync-tools", response_model=ToolSyncResult)
async def sync_tools(
    integration_id: uuid.UUID,
    settings: Settings = Depends(settings_dep),
    principal: Principal = Depends(require_principal("tool.manage")),
    client: ClientInfo = Depends(client_info),
) -> ToolSyncResult:
    """Fetch the MCP server's tool list; new tools arrive disabled for review."""
    return await tool_service.sync_mcp_tools(
        principal, client, settings, principal.tenant_id, integration_id
    )
