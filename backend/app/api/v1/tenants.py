"""Tenant management (platform administrators only)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from app.api.dependencies import require_permission
from app.schemas.common import Page, PageParams
from app.schemas.identity import TenantCreate, TenantOut, TenantUpdate
from app.services import tenant_service
from app.services.context import RequestContext

router = APIRouter(prefix="/tenants", tags=["tenants"])
_manage = require_permission("tenant.manage")


@router.get("", response_model=Page[TenantOut])
async def list_tenants(
    page: PageParams = Depends(), ctx: RequestContext = Depends(_manage)
) -> Page[TenantOut]:
    rows, total = await tenant_service.list_tenants(ctx, page)
    return Page(
        items=[TenantOut.model_validate(t) for t in rows],
        total=total,
        page=page.page,
        page_size=page.page_size,
    )


@router.post("", response_model=TenantOut, status_code=201)
async def create_tenant(body: TenantCreate, ctx: RequestContext = Depends(_manage)) -> TenantOut:
    return TenantOut.model_validate(await tenant_service.create_tenant(ctx, body))


@router.get("/{tenant_id}", response_model=TenantOut)
async def get_tenant(tenant_id: uuid.UUID, ctx: RequestContext = Depends(_manage)) -> TenantOut:
    return TenantOut.model_validate(await tenant_service.get_tenant(ctx, tenant_id))


@router.put("/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    tenant_id: uuid.UUID, body: TenantUpdate, ctx: RequestContext = Depends(_manage)
) -> TenantOut:
    return TenantOut.model_validate(await tenant_service.update_tenant(ctx, tenant_id, body))


@router.post("/{tenant_id}/disable", response_model=TenantOut)
async def disable_tenant(tenant_id: uuid.UUID, ctx: RequestContext = Depends(_manage)) -> TenantOut:
    tenant = await tenant_service.update_tenant(ctx, tenant_id, TenantUpdate(status="disabled"))
    return TenantOut.model_validate(tenant)


@router.post("/{tenant_id}/enable", response_model=TenantOut)
async def enable_tenant(tenant_id: uuid.UUID, ctx: RequestContext = Depends(_manage)) -> TenantOut:
    tenant = await tenant_service.update_tenant(ctx, tenant_id, TenantUpdate(status="active"))
    return TenantOut.model_validate(tenant)
