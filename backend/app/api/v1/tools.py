"""The read-only diagnostic tool catalogue, and running a tool on demand."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.api.dependencies import require_permission, require_principal, settings_dep
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.models import ToolExecution
from app.schemas.incidents import ToolExecutionOut, ToolOut, ToolRunOut, ToolRunRequest
from app.services.context import Principal, RequestContext
from app.tools.executor import run_tool
from app.tools.registry import ALL_TOOLS, TOOLS

router = APIRouter(tags=["tools"])


@router.get("/tools", response_model=list[ToolOut])
async def list_tools(
    resource_type: str | None = Query(None, max_length=30),
    _: RequestContext = Depends(require_permission("incident.read")),
) -> list[ToolOut]:
    specs = [t for t in ALL_TOOLS if resource_type is None or resource_type in t.resource_types]
    return [ToolOut.model_validate(t.describe()) for t in specs]


@router.post("/tools/{name}/run", response_model=ToolRunOut)
async def run_tool_now(
    name: str,
    body: ToolRunRequest,
    settings: Settings = Depends(settings_dep),
    principal: Principal = Depends(require_principal("tool.execute.readonly")),
) -> ToolRunOut:
    """Run one read-only tool against a resource (validated, redacted, audited)."""
    if name not in TOOLS:
        raise NotFoundError(f"Unknown tool '{name}'")
    result = await run_tool(
        settings,
        principal.tenant_id,
        name,
        {**body.arguments, "resource_id": str(body.resource_id)},
        allow_remote=True,
        actor=principal.username,
        user_id=principal.user_id,
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
