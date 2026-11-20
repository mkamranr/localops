"""Approvals, remediation executions and remediation policies (spec §10.6, phase 5)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from redis.asyncio import Redis

from app.api.dependencies import (
    client_info,
    dispatcher_dep,
    redis_dep,
    require_permission,
    require_principal,
)
from app.graphs.dispatch import InvestigationDispatcher
from app.schemas.common import Page, PageParams
from app.schemas.remediation import (
    ApprovalDecision,
    ApprovalDetail,
    ApprovalOut,
    ApprovalStatus,
    ApprovalSummary,
    ChangesRequest,
    ExecutionOut,
    PolicyIn,
    PolicyOut,
    PolicyPreviewIn,
    PolicyUpdate,
    RiskLevel,
)
from app.services import approval_service as svc
from app.services import policy_service
from app.services.context import ClientInfo, Principal, RequestContext
from app.tools import catalogue as tool_catalogue

router = APIRouter(tags=["approvals"])


# ------------------------------------------------------------------ approvals


@router.get("/approvals", response_model=Page[ApprovalOut])
async def list_approvals(
    page: PageParams = Depends(),
    status: ApprovalStatus | None = None,
    risk: RiskLevel | None = None,
    mine: bool = Query(False, description="Only requests from my investigations"),
    investigation_id: uuid.UUID | None = None,
    incident_id: uuid.UUID | None = None,
    ctx: RequestContext = Depends(require_permission("approval.read")),
) -> Page[ApprovalOut]:
    rows, total = await svc.list_approvals(
        ctx,
        page,
        status=status,
        risk=risk,
        mine=mine,
        investigation_id=investigation_id,
        incident_id=incident_id,
    )
    return Page(items=rows, total=total, page=page.page, page_size=page.page_size)


@router.get("/approvals/summary", response_model=ApprovalSummary)
async def approvals_summary(
    ctx: RequestContext = Depends(require_permission("approval.read")),
) -> ApprovalSummary:
    return await svc.summary(ctx)


@router.get("/approvals/{approval_id}", response_model=ApprovalDetail)
async def get_approval(
    approval_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("approval.read")),
) -> ApprovalDetail:
    return await svc.get(ctx, approval_id)


@router.post("/approvals/{approval_id}/approve", response_model=ApprovalOut)
async def approve(
    approval_id: uuid.UUID,
    body: ApprovalDecision | None = None,
    client: ClientInfo = Depends(client_info),
    dispatcher: InvestigationDispatcher = Depends(dispatcher_dep),
    redis: Redis = Depends(redis_dep),
    principal: Principal = Depends(require_principal("approval.approve")),
) -> ApprovalOut:
    comment = body.comment if body else None
    return await svc.decide(principal, client, approval_id, "approve", comment, dispatcher, redis)


@router.post("/approvals/{approval_id}/reject", response_model=ApprovalOut)
async def reject(
    approval_id: uuid.UUID,
    body: ApprovalDecision | None = None,
    client: ClientInfo = Depends(client_info),
    dispatcher: InvestigationDispatcher = Depends(dispatcher_dep),
    redis: Redis = Depends(redis_dep),
    principal: Principal = Depends(require_principal("approval.approve")),
) -> ApprovalOut:
    comment = body.comment if body else None
    return await svc.decide(principal, client, approval_id, "reject", comment, dispatcher, redis)


@router.post("/approvals/{approval_id}/request-changes", response_model=ApprovalOut)
async def request_changes(
    approval_id: uuid.UUID,
    body: ChangesRequest,
    client: ClientInfo = Depends(client_info),
    dispatcher: InvestigationDispatcher = Depends(dispatcher_dep),
    redis: Redis = Depends(redis_dep),
    principal: Principal = Depends(require_principal("approval.approve")),
) -> ApprovalOut:
    return await svc.decide(
        principal, client, approval_id, "request_changes", body.comment, dispatcher, redis
    )


@router.post("/approvals/{approval_id}/cancel", response_model=ApprovalOut)
async def cancel(
    approval_id: uuid.UUID,
    body: ApprovalDecision | None = None,
    client: ClientInfo = Depends(client_info),
    dispatcher: InvestigationDispatcher = Depends(dispatcher_dep),
    redis: Redis = Depends(redis_dep),
    principal: Principal = Depends(require_principal("approval.read")),
) -> ApprovalOut:
    comment = body.comment if body else None
    return await svc.decide(principal, client, approval_id, "cancel", comment, dispatcher, redis)


# ------------------------------------------------------------------ executions


@router.get("/executions", response_model=Page[ExecutionOut])
async def list_executions(
    page: PageParams = Depends(),
    investigation_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    status: str | None = Query(None, pattern="^(running|succeeded|failed|rejected)$"),
    incident_id: uuid.UUID | None = None,
    ctx: RequestContext = Depends(require_permission("approval.read")),
) -> Page[ExecutionOut]:
    rows, total = await svc.list_executions(
        ctx,
        page,
        investigation_id=investigation_id,
        resource_id=resource_id,
        status=status,
        incident_id=incident_id,
    )
    return Page(items=rows, total=total, page=page.page, page_size=page.page_size)


@router.get("/executions/{job_id}", response_model=ExecutionOut)
async def get_execution(
    job_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("approval.read")),
) -> ExecutionOut:
    return await svc.get_execution(ctx, job_id)


# ------------------------------------------------------------------ policies


@router.get("/policies", response_model=list[PolicyOut])
async def list_policies(
    ctx: RequestContext = Depends(require_permission("settings.read")),
) -> list[PolicyOut]:
    return await policy_service.list_policies(ctx)


@router.get("/policies/actions")
async def remediation_actions(
    ctx: RequestContext = Depends(require_permission("settings.read")),
) -> list[dict[str, Any]]:
    """The registered remediation tools that policies can refer to."""
    return [t.describe() for t in (await tool_catalogue.load(ctx.session)).change_tools()]


@router.post("/policies/preview")
async def preview_policy(
    body: PolicyPreviewIn,
    ctx: RequestContext = Depends(require_permission("settings.read")),
) -> dict[str, Any]:
    return await policy_service.preview(ctx, body)


@router.post("/policies", response_model=PolicyOut, status_code=201)
async def create_policy(
    body: PolicyIn,
    ctx: RequestContext = Depends(require_permission("settings.manage")),
) -> PolicyOut:
    return await policy_service.create(ctx, body)


@router.put("/policies/{policy_id}", response_model=PolicyOut)
async def update_policy(
    policy_id: uuid.UUID,
    body: PolicyUpdate,
    ctx: RequestContext = Depends(require_permission("settings.manage")),
) -> PolicyOut:
    return await policy_service.update(ctx, policy_id, body)


@router.delete("/policies/{policy_id}", status_code=204)
async def delete_policy(
    policy_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("settings.manage")),
) -> Response:
    await policy_service.delete(ctx, policy_id)
    return Response(status_code=204)
