"""Knowledge documents: CRUD, upload, versions, validation, feedback, search, stats.

Mutations that need (re)indexing commit first and then hand the document to the
worker, so the index job always sees the committed row. Documents generated from
incidents and investigations are kept in sync by the worker; their content is not
editable here (only status, tags and category).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.logging import get_logger
from app.database.session import tenant_session
from app.graphs.dispatch import InvestigationDispatcher
from app.knowledge import vectorstore
from app.knowledge.extract import extract_text
from app.knowledge.indexer import content_hash
from app.knowledge.search import hybrid_search
from app.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeFeedback,
    ModelEndpoint,
    Resource,
)
from app.schemas.common import PageParams
from app.schemas.knowledge import (
    ChunkOut,
    DocumentCreate,
    DocumentDetail,
    DocumentOut,
    DocumentUpdate,
    FeedbackIn,
    KnowledgeStats,
    SearchRequest,
    SearchResponse,
    VersionDetail,
    VersionOut,
)
from app.services import audit_service as audit
from app.services.context import ClientInfo, Principal, RequestContext
from app.services.model_service import default_embedding_endpoint

log = get_logger("localops.knowledge")

GENERATED_SOURCES = ("incident", "investigation")


# ------------------------------------------------------------------ helpers


async def _get(session: AsyncSession, document_id: uuid.UUID) -> KnowledgeDocument:
    doc = await session.get(KnowledgeDocument, document_id)
    if doc is None:
        raise NotFoundError("Knowledge document not found")
    return doc


async def _check_resources(session: AsyncSession, resource_ids: list[uuid.UUID]) -> None:
    if not resource_ids:
        return
    found = set(await session.scalars(select(Resource.id).where(Resource.id.in_(resource_ids))))
    missing = [str(r) for r in resource_ids if r not in found]
    if missing:
        raise NotFoundError("Resource not found", details={"resource_ids": missing})


async def _feedback_counts(
    session: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int]]:
    if not ids:
        return {}
    rows = await session.execute(
        select(
            KnowledgeFeedback.document_id,
            func.count(case((KnowledgeFeedback.helpful.is_(True), 1))),
            func.count(case((KnowledgeFeedback.helpful.is_(False), 1))),
        )
        .where(KnowledgeFeedback.document_id.in_(ids))
        .group_by(KnowledgeFeedback.document_id)
    )
    return {d: (int(y), int(n)) for d, y, n in rows.all()}


def _out(
    doc: KnowledgeDocument, chunks: int = 0, feedback: tuple[int, int] = (0, 0)
) -> DocumentOut:
    out = DocumentOut.model_validate(doc)
    out.size = len(doc.content)
    out.filename = (doc.metadata_ or {}).get("filename")
    out.chunk_count = chunks
    out.helpful, out.not_helpful = feedback
    return out


async def _chunk_counts(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not ids:
        return {}
    rows = await session.execute(
        select(KnowledgeChunk.document_id, func.count())
        .where(KnowledgeChunk.document_id.in_(ids))
        .group_by(KnowledgeChunk.document_id)
    )
    return {d: int(n) for d, n in rows.all()}


# ------------------------------------------------------------------ queries


async def list_documents(
    ctx: RequestContext,
    page: PageParams,
    *,
    q: str | None = None,
    document_type: str | None = None,
    status: str | None = None,
    source: str | None = None,
    index_status: str | None = None,
    resource_id: uuid.UUID | None = None,
    tag: str | None = None,
) -> tuple[list[DocumentOut], int]:
    s = ctx.session
    stmt = select(KnowledgeDocument)
    if q:
        like = f"%{q.strip()[:100]}%"
        stmt = stmt.where(KnowledgeDocument.title.ilike(like))
    if document_type:
        stmt = stmt.where(KnowledgeDocument.document_type == document_type)
    if status:
        stmt = stmt.where(KnowledgeDocument.status == status)
    if source:
        stmt = stmt.where(KnowledgeDocument.source == source)
    if index_status:
        stmt = stmt.where(KnowledgeDocument.index_status == index_status)
    if resource_id:
        stmt = stmt.where(KnowledgeDocument.resource_ids.contains([resource_id]))
    if tag:
        stmt = stmt.where(KnowledgeDocument.tags.contains([tag.lower()]))
    total = int(await s.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    docs = list(
        await s.scalars(
            stmt.order_by(
                func.coalesce(KnowledgeDocument.updated_at, KnowledgeDocument.created_at).desc()
            )
            .offset(page.offset)
            .limit(page.page_size)
        )
    )
    ids = [d.id for d in docs]
    counts, fb = await _chunk_counts(s, ids), await _feedback_counts(s, ids)
    return [_out(d, counts.get(d.id, 0), fb.get(d.id, (0, 0))) for d in docs], total


async def get_document(ctx: RequestContext, document_id: uuid.UUID) -> DocumentDetail:
    s = ctx.session
    doc = await _get(s, document_id)
    chunks = list(
        await s.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == doc.id)
            .order_by(KnowledgeChunk.chunk_index)
        )
    )
    fb = (await _feedback_counts(s, [doc.id])).get(doc.id, (0, 0))
    base = _out(doc, len(chunks), fb)
    names: dict[str, str] = {}
    if doc.resource_ids:
        rows = await s.execute(
            select(Resource.id, Resource.name).where(Resource.id.in_(doc.resource_ids))
        )
        names = {str(i): n for i, n in rows.all()}
    return DocumentDetail(
        **base.model_dump(),
        content=doc.content,
        metadata=doc.metadata_ or {},
        chunks=[
            ChunkOut(
                id=c.id,
                chunk_index=c.chunk_index,
                heading=(c.metadata_ or {}).get("heading", ""),
                content=c.content,
                embedded=bool(c.embedding_id),
            )
            for c in chunks
        ],
        resource_names=names,
    )


async def list_versions(ctx: RequestContext, document_id: uuid.UUID) -> list[VersionOut]:
    await _get(ctx.session, document_id)
    rows = await ctx.session.scalars(
        select(KnowledgeDocumentVersion)
        .where(KnowledgeDocumentVersion.document_id == document_id)
        .order_by(KnowledgeDocumentVersion.version.desc())
    )
    out = []
    for v in rows:
        item = VersionOut.model_validate(v)
        item.size = len(v.content)
        out.append(item)
    return out


async def get_version(ctx: RequestContext, document_id: uuid.UUID, version: int) -> VersionDetail:
    v = await ctx.session.scalar(
        select(KnowledgeDocumentVersion).where(
            KnowledgeDocumentVersion.document_id == document_id,
            KnowledgeDocumentVersion.version == version,
        )
    )
    if v is None:
        raise NotFoundError("Version not found")
    out = VersionDetail.model_validate(v)
    out.size = len(v.content)
    return out


# ------------------------------------------------------------------ mutations


async def _create(
    s: AsyncSession,
    principal: Principal,
    client: ClientInfo,
    data: DocumentCreate,
    *,
    source: str,
    metadata: dict[str, Any],
) -> KnowledgeDocument:
    await _check_resources(s, data.resource_ids)
    title = data.title.strip()
    doc = KnowledgeDocument(
        tenant_id=principal.tenant_id,
        title=title,
        document_type=data.document_type,
        source=source,
        content=data.content,
        content_hash=content_hash(title, data.content),
        metadata_=metadata,
        category=(data.category or "").strip() or None,
        tags=data.tags,
        resource_ids=list(dict.fromkeys(data.resource_ids)),
        status=data.status,
        created_by=principal.username,
    )
    s.add(doc)
    await s.flush()
    s.add(
        KnowledgeDocumentVersion(
            tenant_id=principal.tenant_id,
            document_id=doc.id,
            version=1,
            title=title,
            content=data.content,
            created_by=principal.username,
            note="Uploaded" if source == "upload" else "Created",
        )
    )
    await audit.record(
        s,
        tenant_id=principal.tenant_id,
        actor=principal,
        action="knowledge.upload" if source == "upload" else "knowledge.create",
        entity_type="knowledge_document",
        entity_id=doc.id,
        details={"title": title, "type": doc.document_type, **metadata},
        client=client,
    )
    return doc


async def create_document(
    principal: Principal,
    client: ClientInfo,
    dispatcher: InvestigationDispatcher,
    data: DocumentCreate,
) -> uuid.UUID:
    async with tenant_session(principal.tenant_id) as s:
        doc = await _create(s, principal, client, data, source="manual", metadata={})
        doc_id = doc.id
    await dispatcher.enqueue_index(principal.tenant_id, doc_id)
    return doc_id


async def upload_document(
    principal: Principal,
    client: ClientInfo,
    dispatcher: InvestigationDispatcher,
    settings: Settings,
    *,
    filename: str,
    data: bytes,
    title: str | None,
    document_type: str,
    category: str | None,
    tags: list[str],
    resource_ids: list[uuid.UUID],
    status: str,
) -> uuid.UUID:
    limit = settings.knowledge_max_upload_mb * 1024 * 1024
    if len(data) > limit:
        raise ValidationFailedError(
            f"File exceeds the {settings.knowledge_max_upload_mb} MB upload limit"
        )
    text, mime = extract_text(filename, data)
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:200]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    payload = DocumentCreate.model_validate(
        {
            "title": (title or "").strip() or stem or "Untitled document",
            "document_type": document_type,
            "content": text,
            "category": category,
            "tags": tags,
            "resource_ids": resource_ids,
            "status": status,
        }
    )
    async with tenant_session(principal.tenant_id) as s:
        doc = await _create(
            s,
            principal,
            client,
            payload,
            source="upload",
            metadata={"filename": base, "mime": mime, "bytes": len(data)},
        )
        doc_id = doc.id
    await dispatcher.enqueue_index(principal.tenant_id, doc_id)
    return doc_id


async def update_document(
    principal: Principal,
    client: ClientInfo,
    dispatcher: InvestigationDispatcher,
    document_id: uuid.UUID,
    data: DocumentUpdate,
) -> None:
    reindex = False
    async with tenant_session(principal.tenant_id) as s:
        doc = await _get(s, document_id)
        generated = doc.source in GENERATED_SOURCES
        if generated and (
            data.content is not None or data.title is not None or data.document_type is not None
        ):
            raise ValidationFailedError(
                "This document is generated from an "
                f"{doc.source}; edit the {doc.source} instead (status and tags can change)"
            )
        changes: dict[str, Any] = {}
        new_title = data.title.strip() if data.title is not None else doc.title
        new_content = data.content if data.content is not None else doc.content
        if new_title != doc.title or new_content != doc.content:
            doc.version += 1
            changes["version"] = doc.version
            if new_title != doc.title:
                changes["title"] = new_title
            if new_content != doc.content:
                changes["content"] = f"{len(doc.content)} → {len(new_content)} characters"
            doc.title, doc.content = new_title, new_content
            doc.content_hash = content_hash(new_title, new_content)
            # Changed content needs re-validation.
            doc.validated_at, doc.validated_by = None, None
            s.add(
                KnowledgeDocumentVersion(
                    tenant_id=principal.tenant_id,
                    document_id=doc.id,
                    version=doc.version,
                    title=new_title,
                    content=new_content,
                    created_by=principal.username,
                    note=data.note,
                )
            )
            reindex = True
        if data.resource_ids is not None:
            ids = list(dict.fromkeys(data.resource_ids))
            await _check_resources(s, ids)
            if ids != list(doc.resource_ids):
                doc.resource_ids = ids
                changes["resource_ids"] = [str(i) for i in ids]
                reindex = True
        for field in ("document_type", "status"):
            value = getattr(data, field)
            if value is not None and value != getattr(doc, field):
                setattr(doc, field, value)
                changes[field] = value
                reindex = True  # both are part of the vector payload
        if data.tags is not None and data.tags != list(doc.tags):
            doc.tags = changes["tags"] = data.tags
        if data.category is not None and (data.category.strip() or None) != doc.category:
            doc.category = changes["category"] = data.category.strip() or None
        if reindex:
            doc.index_status = "pending"
        if changes:
            await audit.record(
                s,
                tenant_id=principal.tenant_id,
                actor=principal,
                action="knowledge.update",
                entity_type="knowledge_document",
                entity_id=doc.id,
                details={"title": doc.title, "changes": changes},
                client=client,
            )
    if reindex:
        await dispatcher.enqueue_index(principal.tenant_id, document_id)


async def restore_version(
    principal: Principal,
    client: ClientInfo,
    dispatcher: InvestigationDispatcher,
    document_id: uuid.UUID,
    version: int,
) -> None:
    async with tenant_session(principal.tenant_id) as s:
        doc = await _get(s, document_id)
        if doc.source in GENERATED_SOURCES:
            raise ValidationFailedError("Generated documents cannot be restored")
        old = await s.scalar(
            select(KnowledgeDocumentVersion).where(
                KnowledgeDocumentVersion.document_id == document_id,
                KnowledgeDocumentVersion.version == version,
            )
        )
        if old is None:
            raise NotFoundError("Version not found")
        title, content = old.title, old.content
    await update_document(
        principal,
        client,
        dispatcher,
        document_id,
        DocumentUpdate(title=title, content=content, note=f"Restored version {version}"),
    )


async def delete_document(
    principal: Principal, client: ClientInfo, settings: Settings, document_id: uuid.UUID
) -> None:
    async with tenant_session(principal.tenant_id) as s:
        doc = await _get(s, document_id)
        title = doc.title
        await s.delete(doc)
        await audit.record(
            s,
            tenant_id=principal.tenant_id,
            actor=principal,
            action="knowledge.delete",
            entity_type="knowledge_document",
            entity_id=document_id,
            details={"title": title},
            client=client,
        )
    try:
        await vectorstore.delete_document(settings, principal.tenant_id, document_id)
    except Exception as exc:
        log.warning("vector_delete_failed", document_id=str(document_id), error=str(exc)[:200])


async def validate_document(ctx: RequestContext, document_id: uuid.UUID, validated: bool) -> None:
    s, p = ctx.session, ctx.principal
    doc = await _get(s, document_id)
    if validated:
        doc.validated_at, doc.validated_by = datetime.now(UTC), p.username
    else:
        doc.validated_at, doc.validated_by = None, None
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="knowledge.validate" if validated else "knowledge.unvalidate",
        entity_type="knowledge_document",
        entity_id=doc.id,
        details={"title": doc.title, "version": doc.version},
        client=ctx.client,
    )


async def add_feedback(ctx: RequestContext, document_id: uuid.UUID, data: FeedbackIn) -> None:
    s, p = ctx.session, ctx.principal
    doc = await _get(s, document_id)
    # One vote per user and document: a new vote replaces the previous one.
    previous = await s.scalars(
        select(KnowledgeFeedback).where(
            KnowledgeFeedback.document_id == doc.id, KnowledgeFeedback.user_id == p.user_id
        )
    )
    for row in previous:
        await s.delete(row)
    s.add(
        KnowledgeFeedback(
            tenant_id=p.tenant_id,
            document_id=doc.id,
            chunk_id=data.chunk_id,
            investigation_id=data.investigation_id,
            user_id=p.user_id,
            username=p.username,
            helpful=data.helpful,
            comment=data.comment,
        )
    )
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="knowledge.feedback",
        entity_type="knowledge_document",
        entity_id=doc.id,
        details={"helpful": data.helpful},
        client=ctx.client,
    )


async def mark_for_reindex(
    principal: Principal,
    client: ClientInfo,
    dispatcher: InvestigationDispatcher,
    document_ids: list[uuid.UUID] | None,
) -> int:
    async with tenant_session(principal.tenant_id) as s:
        stmt = select(KnowledgeDocument)
        if document_ids:
            stmt = stmt.where(KnowledgeDocument.id.in_(document_ids))
        docs = list(await s.scalars(stmt))
        for doc in docs:
            doc.index_status = "pending"
        ids = [d.id for d in docs]
        await audit.record(
            s,
            tenant_id=principal.tenant_id,
            actor=principal,
            action="knowledge.reindex",
            entity_type="knowledge_document",
            details={"documents": len(ids), "all": not document_ids},
            client=client,
        )
    for doc_id in ids:
        await dispatcher.enqueue_index(principal.tenant_id, doc_id)
    return len(ids)


# ------------------------------------------------------------------ search & stats


async def search(ctx: RequestContext, settings: Settings, req: SearchRequest) -> SearchResponse:
    result = await hybrid_search(
        ctx.session,
        settings,
        ctx.principal.tenant_id,
        req.query,
        document_types=list(req.document_types) if req.document_types else None,
        resource_id=req.resource_id,
        boost_resources=[req.resource_id] if req.resource_id else None,
        tags=[t.lower() for t in req.tags] if req.tags else None,
        include_deprecated=req.include_deprecated,
        limit=req.limit,
    )
    return SearchResponse.model_validate(
        {
            "mode": result.mode,
            "note": result.note,
            "hits": [h.as_dict() for h in result.hits],
        }
    )


async def stats(ctx: RequestContext, settings: Settings) -> KnowledgeStats:
    s = ctx.session

    async def grouped(column: Any) -> dict[str, int]:
        rows = await s.execute(select(column, func.count()).group_by(column))
        return {str(k): int(n) for k, n in rows.all()}

    by_type = await grouped(KnowledgeDocument.document_type)
    endpoint = await default_embedding_endpoint(s, settings)
    model = (
        await s.scalar(
            select(ModelEndpoint.model_name).where(
                ModelEndpoint.purpose == "embedding",
                ModelEndpoint.is_default.is_(True),
                ModelEndpoint.enabled.is_(True),
            )
        )
        if endpoint
        else None
    )
    vector = (
        "not_configured"
        if endpoint is None
        else "ok"
        if await vectorstore.healthy(settings)
        else "unavailable"
    )
    return KnowledgeStats(
        documents=sum(by_type.values()),
        chunks=int(await s.scalar(select(func.count()).select_from(KnowledgeChunk)) or 0),
        by_type=by_type,
        by_status=await grouped(KnowledgeDocument.status),
        by_index_status=await grouped(KnowledgeDocument.index_status),
        validated=int(
            await s.scalar(
                select(func.count())
                .select_from(KnowledgeDocument)
                .where(KnowledgeDocument.validated_at.is_not(None))
            )
            or 0
        ),
        embedding_model=model,
        vector_store=vector,
    )
