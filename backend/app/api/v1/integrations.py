"""Integrations: connection profiles, tests, discovery runs and SSH host keys."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response

from app.api.dependencies import (
    client_info,
    dispatcher_dep,
    require_permission,
    require_principal,
    settings_dep,
)
from app.core.config import Settings
from app.graphs.dispatch import InvestigationDispatcher
from app.schemas.common import Page, PageParams
from app.schemas.inventory import (
    DiscoveryRunOut,
    HostKeyOut,
    IntegrationCreate,
    IntegrationOut,
    IntegrationTestRequest,
    IntegrationTestResult,
    IntegrationUpdate,
)
from app.services import integration_service as svc
from app.services.context import ClientInfo, Principal, RequestContext

router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.get("", response_model=list[IntegrationOut])
async def list_integrations(
    ctx: RequestContext = Depends(require_permission("integration.read")),
) -> list[IntegrationOut]:
    return await svc.list_integrations(ctx.session)


@router.post("", response_model=IntegrationOut, status_code=201)
async def create_integration(
    body: IntegrationCreate,
    settings: Settings = Depends(settings_dep),
    ctx: RequestContext = Depends(require_permission("integration.configure")),
) -> IntegrationOut:
    integration = await svc.create_integration(ctx, settings, body)
    return svc.to_out(integration)


@router.get("/runs/{run_id}", response_model=DiscoveryRunOut)
async def get_run(
    run_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("integration.read")),
) -> DiscoveryRunOut:
    return DiscoveryRunOut.model_validate(await svc.get_run(ctx.session, run_id))


@router.get("/{integration_id}", response_model=IntegrationOut)
async def get_integration(
    integration_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("integration.read")),
) -> IntegrationOut:
    integration = await svc.get_integration(ctx.session, integration_id)
    return await svc.integration_out(ctx.session, integration)


@router.put("/{integration_id}", response_model=IntegrationOut)
async def update_integration(
    integration_id: uuid.UUID,
    body: IntegrationUpdate,
    settings: Settings = Depends(settings_dep),
    ctx: RequestContext = Depends(require_permission("integration.configure")),
) -> IntegrationOut:
    integration = await svc.update_integration(ctx, settings, integration_id, body)
    return await svc.integration_out(ctx.session, integration)


@router.delete("/{integration_id}", status_code=204)
async def delete_integration(
    integration_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("integration.configure")),
) -> Response:
    await svc.delete_integration(ctx, integration_id)
    return Response(status_code=204)


@router.post("/{integration_id}/test", response_model=IntegrationTestResult)
async def test_integration(
    integration_id: uuid.UUID,
    body: IntegrationTestRequest | None = None,
    settings: Settings = Depends(settings_dep),
    client: ClientInfo = Depends(client_info),
    principal: Principal = Depends(require_principal("integration.configure")),
) -> IntegrationTestResult:
    return await svc.test_integration(
        principal, client, settings, integration_id, body.target if body else None
    )


@router.post("/{integration_id}/discover", response_model=DiscoveryRunOut, status_code=202)
async def discover(
    integration_id: uuid.UUID,
    client: ClientInfo = Depends(client_info),
    dispatcher: InvestigationDispatcher = Depends(dispatcher_dep),
    principal: Principal = Depends(require_principal("integration.configure")),
) -> DiscoveryRunOut:
    run = await svc.start_discovery(principal, client, integration_id, dispatcher)
    return DiscoveryRunOut.model_validate(run)


@router.get("/{integration_id}/runs", response_model=Page[DiscoveryRunOut])
async def list_runs(
    integration_id: uuid.UUID,
    page: PageParams = Depends(),
    ctx: RequestContext = Depends(require_permission("integration.read")),
) -> Page[DiscoveryRunOut]:
    rows, total = await svc.list_runs(ctx.session, integration_id, page)
    return Page(
        items=[DiscoveryRunOut.model_validate(r) for r in rows],
        total=total,
        page=page.page,
        page_size=page.page_size,
    )


@router.get("/{integration_id}/host-keys", response_model=list[HostKeyOut])
async def list_host_keys(
    integration_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("integration.read")),
) -> list[HostKeyOut]:
    return [
        HostKeyOut.model_validate(k) for k in await svc.list_host_keys(ctx.session, integration_id)
    ]


@router.delete("/{integration_id}/host-keys/{key_id}", status_code=204)
async def reset_host_key(
    integration_id: uuid.UUID,
    key_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("integration.configure")),
) -> Response:
    await svc.reset_host_key(ctx, integration_id, key_id)
    return Response(status_code=204)
