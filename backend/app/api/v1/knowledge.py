"""Knowledge base (spec §10.9): documents, upload, versions, search, re-index, stats."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status

from app.api.dependencies import (
    client_info,
    dispatcher_dep,
    require_permission,
    require_principal,
    settings_dep,
)
from app.core.config import Settings
from app.core.errors import ValidationFailedError
from app.database.session import tenant_session
from app.graphs.dispatch import InvestigationDispatcher
from app.schemas.common import Page, PageParams
from app.schemas.knowledge import (
    DocumentCreate,
    DocumentDetail,
    DocumentOut,
    DocumentStatus,
    DocumentType,
    DocumentUpdate,
    EditableType,
    FeedbackIn,
    IndexStatus,
    KnowledgeStats,
    ReindexRequest,
    ReindexResult,
    SearchRequest,
    SearchResponse,
    ValidateIn,
    VersionDetail,
    VersionOut,
)
from app.services import knowledge_service as svc
from app.services.context import ClientInfo, Principal, RequestContext

router = APIRouter(tags=["knowledge"])


async def _detail(
    principal: Principal, client: ClientInfo, document_id: uuid.UUID
) -> DocumentDetail:
    """Read back after the service committed (fresh transaction)."""
    async with tenant_session(principal.tenant_id) as s:
        ctx = RequestContext(session=s, principal=principal, client=client)
        return await svc.get_document(ctx, document_id)


@router.get("/knowledge", response_model=Page[DocumentOut])
async def list_documents(
    page: PageParams = Depends(),
    q: str | None = Query(None, max_length=100),
    document_type: DocumentType | None = None,
    status: DocumentStatus | None = None,
    source: str | None = Query(None, pattern="^(upload|manual|incident|investigation)$"),
    index_status: IndexStatus | None = None,
    resource_id: uuid.UUID | None = None,
    tag: str | None = Query(None, max_length=60),
    ctx: RequestContext = Depends(require_permission("knowledge.read")),
) -> Page[DocumentOut]:
    items, total = await svc.list_documents(
        ctx,
        page,
        q=q,
        document_type=document_type,
        status=status,
        source=source,
        index_status=index_status,
        resource_id=resource_id,
        tag=tag,
    )
    return Page(items=items, total=total, page=page.page, page_size=page.page_size)


@router.get("/knowledge/stats", response_model=KnowledgeStats)
async def knowledge_stats(
    settings: Settings = Depends(settings_dep),
    ctx: RequestContext = Depends(require_permission("knowledge.read")),
) -> KnowledgeStats:
    return await svc.stats(ctx, settings)


@router.post("/knowledge/search", response_model=SearchResponse)
async def search_knowledge(
    body: SearchRequest,
    settings: Settings = Depends(settings_dep),
    ctx: RequestContext = Depends(require_permission("knowledge.read")),
) -> SearchResponse:
    return await svc.search(ctx, settings, body)


@router.post("/knowledge", response_model=DocumentDetail, status_code=status.HTTP_201_CREATED)
async def create_document(
    body: DocumentCreate,
    principal: Principal = Depends(require_principal("knowledge.manage")),
    client: ClientInfo = Depends(client_info),
    dispatcher: InvestigationDispatcher = Depends(dispatcher_dep),
) -> DocumentDetail:
    doc_id = await svc.create_document(principal, client, dispatcher, body)
    return await _detail(principal, client, doc_id)


def _list_field(raw: str | None, name: str) -> list[str]:
    """Multipart list fields arrive as JSON arrays or comma-separated text."""
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationFailedError(f"Invalid {name}: {exc.msg}") from exc
        if not isinstance(value, list):
            raise ValidationFailedError(f"Invalid {name}")
        return [str(v) for v in value]
    return [p.strip() for p in raw.split(",") if p.strip()]


@router.post(
    "/knowledge/upload", response_model=DocumentDetail, status_code=status.HTTP_201_CREATED
)
async def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(None, max_length=300),
    document_type: EditableType = Form("runbook"),
    category: str | None = Form(None, max_length=100),
    tags: str | None = Form(None, max_length=2000),
    resource_ids: str | None = Form(None, max_length=5000),
    doc_status: str = Form("active", alias="status", pattern="^(active|draft)$"),
    settings: Settings = Depends(settings_dep),
    principal: Principal = Depends(require_principal("knowledge.manage")),
    client: ClientInfo = Depends(client_info),
    dispatcher: InvestigationDispatcher = Depends(dispatcher_dep),
) -> DocumentDetail:
    limit = settings.knowledge_max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    try:
        ids = [uuid.UUID(v) for v in _list_field(resource_ids, "resource_ids")]
    except ValueError as exc:
        raise ValidationFailedError("resource_ids must be UUIDs") from exc
    doc_id = await svc.upload_document(
        principal,
        client,
        dispatcher,
        settings,
        filename=file.filename or "document.txt",
        data=data,
        title=title,
        document_type=document_type,
        category=category,
        tags=_list_field(tags, "tags"),
        resource_ids=ids,
        status=doc_status,
    )
    return await _detail(principal, client, doc_id)


@router.post("/knowledge/reindex", response_model=ReindexResult)
async def reindex(
    body: ReindexRequest,
    principal: Principal = Depends(require_principal("knowledge.manage")),
    client: ClientInfo = Depends(client_info),
    dispatcher: InvestigationDispatcher = Depends(dispatcher_dep),
) -> ReindexResult:
    return ReindexResult(
        queued=await svc.mark_for_reindex(principal, client, dispatcher, body.document_ids)
    )


@router.get("/knowledge/{document_id}", response_model=DocumentDetail)
async def get_document(
    document_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("knowledge.read")),
) -> DocumentDetail:
    return await svc.get_document(ctx, document_id)


@router.put("/knowledge/{document_id}", response_model=DocumentDetail)
async def update_document(
    document_id: uuid.UUID,
    body: DocumentUpdate,
    principal: Principal = Depends(require_principal("knowledge.manage")),
    client: ClientInfo = Depends(client_info),
    dispatcher: InvestigationDispatcher = Depends(dispatcher_dep),
) -> DocumentDetail:
    await svc.update_document(principal, client, dispatcher, document_id, body)
    return await _detail(principal, client, document_id)


@router.delete("/knowledge/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    settings: Settings = Depends(settings_dep),
    principal: Principal = Depends(require_principal("knowledge.manage")),
    client: ClientInfo = Depends(client_info),
) -> Response:
    await svc.delete_document(principal, client, settings, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/knowledge/{document_id}/versions", response_model=list[VersionOut])
async def list_versions(
    document_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("knowledge.read")),
) -> list[VersionOut]:
    return await svc.list_versions(ctx, document_id)


@router.get("/knowledge/{document_id}/versions/{version}", response_model=VersionDetail)
async def get_version(
    document_id: uuid.UUID,
    version: int,
    ctx: RequestContext = Depends(require_permission("knowledge.read")),
) -> VersionDetail:
    return await svc.get_version(ctx, document_id, version)


@router.post("/knowledge/{document_id}/versions/{version}/restore", response_model=DocumentDetail)
async def restore_version(
    document_id: uuid.UUID,
    version: int,
    principal: Principal = Depends(require_principal("knowledge.manage")),
    client: ClientInfo = Depends(client_info),
    dispatcher: InvestigationDispatcher = Depends(dispatcher_dep),
) -> DocumentDetail:
    await svc.restore_version(principal, client, dispatcher, document_id, version)
    return await _detail(principal, client, document_id)


@router.post("/knowledge/{document_id}/validate", response_model=DocumentDetail)
async def validate_document(
    document_id: uuid.UUID,
    body: ValidateIn,
    ctx: RequestContext = Depends(require_permission("knowledge.manage")),
) -> DocumentDetail:
    await svc.validate_document(ctx, document_id, body.validated)
    return await svc.get_document(ctx, document_id)


@router.post("/knowledge/{document_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def add_feedback(
    document_id: uuid.UUID,
    body: FeedbackIn,
    ctx: RequestContext = Depends(require_permission("knowledge.read")),
) -> Response:
    await svc.add_feedback(ctx, document_id, body)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
