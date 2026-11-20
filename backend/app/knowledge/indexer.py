"""Index knowledge documents: chunks + full-text in PostgreSQL, embeddings in Qdrant.

Also turns resolved incidents and completed investigations into knowledge documents,
so "similar incident" retrieval works semantically and not only by shared words.

Indexing never holds a database transaction open while the embedding model runs:
chunks are written first (keyword search works immediately), vectors second. If the
embedding endpoint or Qdrant is unavailable the document stays ``keyword_only``.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.database.session import get_engine, tenant_session
from app.knowledge import vectorstore
from app.knowledge.chunking import chunk_text
from app.llm.gateway import EndpointConfig, ModelClient, ModelError
from app.models import (
    Incident,
    IncidentResource,
    Investigation,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    incident_key,
)
from app.services.model_service import default_embedding_endpoint

log = get_logger("localops.knowledge.indexer")

ClientFactory = Callable[[EndpointConfig], ModelClient]
EMBED_INPUT_CHARS = 6000  # per chunk sent to the embedding model


def content_hash(title: str, content: str) -> str:
    return hashlib.sha256(f"{title}\n\n{content}".encode()).hexdigest()


def _embed_text(title: str, heading: str, content: str) -> str:
    head = f"{title} — {heading}" if heading and heading != title else title
    return f"{head}\n\n{content}"[:EMBED_INPUT_CHARS]


async def index_document(
    settings: Settings,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    client_factory: ClientFactory = ModelClient,
) -> str:
    """(Re)build one document's chunks and vectors. Returns the resulting index status."""
    # 1. chunks + full-text (short transaction)
    async with tenant_session(tenant_id) as s:
        doc = await s.get(KnowledgeDocument, document_id, with_for_update=True)
        if doc is None:
            return "missing"
        pieces = chunk_text(
            doc.content,
            size=settings.knowledge_chunk_chars,
            overlap=settings.knowledge_chunk_overlap,
        )
        await s.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id))
        chunks = [
            KnowledgeChunk(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                document_id=doc.id,
                chunk_index=p.index,
                content=p.content,
                metadata_={"heading": p.heading[:300]} if p.heading else {},
            )
            for p in pieces
        ]
        s.add_all(chunks)
        indexed_hash = doc.content_hash
        title = doc.title
        payload = {
            "document_id": str(doc.id),
            "document_type": doc.document_type,
            "resource_ids": [str(r) for r in doc.resource_ids],
            "status": doc.status,
        }
        inputs = [
            (c.id, _embed_text(title, p.heading, p.content))
            for c, p in zip(chunks, pieces, strict=True)
        ]
        endpoint = await default_embedding_endpoint(s, settings)
        doc.index_status = "pending"
        doc.index_error = None

    # 2. vectors (no transaction held)
    status, error, model = "keyword_only", None, None
    if endpoint is None:
        error = "No embedding model is configured; keyword search only"
    else:
        config, fingerprint = endpoint
        model = config.model
        try:
            vectors = await client_factory(config).embed([t for _, t in inputs]) if inputs else []
            await vectorstore.delete_document(settings, tenant_id, document_id)
            if vectors:
                name = vectorstore.collection_name(settings, fingerprint, len(vectors[0]))
                await vectorstore.ensure_collection(settings, name, len(vectors[0]))
                await vectorstore.upsert_chunks(
                    settings,
                    name,
                    tenant_id,
                    [(cid, vec, payload) for (cid, _), vec in zip(inputs, vectors, strict=True)],
                )
            status = "indexed"
        except ModelError as exc:
            error = f"Embedding failed: {exc}"
        except Exception as exc:
            error = f"Vector store unavailable: {type(exc).__name__}"
            log.warning("vector_index_failed", document_id=str(document_id), error=str(exc)[:300])

    # 3. record the outcome — unless the document changed meanwhile (re-indexed later)
    async with tenant_session(tenant_id) as s:
        doc = await s.get(KnowledgeDocument, document_id)
        if doc is None:
            return "missing"
        if doc.content_hash != indexed_hash:
            doc.index_status = "pending"
            return "pending"
        doc.index_status = status
        doc.index_error = error[:1000] if error else None
        doc.indexed_hash = indexed_hash
        doc.embedding_model = model if status == "indexed" else None
        doc.indexed_at = datetime.now(UTC)
        if status == "indexed":
            for c in await s.scalars(
                select(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id)
            ):
                c.embedding_id = str(c.id)
    log.info("knowledge_indexed", document_id=str(document_id), status=status, chunks=len(inputs))
    return status


# ----------------------------------------------------------------- source documents


def _incident_markdown(inc: Incident) -> str:
    root = inc.root_cause or {}
    lines = [
        f"# {incident_key(inc.number)}: {inc.title}",
        f"Severity: {inc.severity} · Priority: {inc.priority} · Status: {inc.status}",
    ]
    if inc.description:
        lines += ["", "## Description", inc.description]
    if root.get("statement"):
        lines += ["", "## Root cause", str(root["statement"])]
        if root.get("confidence") is not None:
            lines.append(f"Confidence: {root.get('confidence')}")
    factors = root.get("contributing_factors") or []
    if factors:
        lines += ["", "## Contributing factors"]
        lines += [f"- {f.get('statement', f) if isinstance(f, dict) else f}" for f in factors]
    actions = root.get("recommended_remediation") or []
    if actions:
        lines += ["", "## Recommended remediation"]
        lines += [f"- {a.get('action', a) if isinstance(a, dict) else a}" for a in actions]
    if inc.investigation_summary:
        lines += ["", "## Investigation summary", inc.investigation_summary]
    if inc.resolution:
        lines += ["", "## Resolution", inc.resolution]
    return "\n".join(lines)


def _investigation_markdown(inv: Investigation) -> str:
    r = inv.result or {}
    root = r.get("root_cause") or {}
    lines = [f"# Investigation: {inv.title}", "", "## Request", inv.request]
    if r.get("incident_summary"):
        lines += ["", "## Summary", str(r["incident_summary"])]
    if r.get("symptoms"):
        lines += ["", "## Symptoms"] + [f"- {s}" for s in r["symptoms"]]
    if root.get("statement"):
        lines += [
            "",
            "## Root cause",
            f"{root['statement']} (status: {root.get('status')}, "
            f"confidence: {root.get('confidence')})",
        ]
    actions = r.get("recommended_remediation") or []
    if actions:
        lines += ["", "## Recommended remediation"]
        lines += [f"- {a.get('action', a) if isinstance(a, dict) else a}" for a in actions]
    rem = r.get("remediation") or {}
    done = [s for s in rem.get("steps") or [] if s.get("status") in ("verified", "executed")]
    if done:
        lines += ["", "## Remediation performed"]
        lines += [f"- {s.get('description')} → {s.get('status')}" for s in done]
    if r.get("conclusion"):
        lines += ["", "## Conclusion", str(r["conclusion"])]
    return "\n".join(lines)


async def _upsert_source_document(
    s: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    source: str,
    source_id: uuid.UUID,
    title: str,
    content: str,
    resource_ids: list[uuid.UUID],
) -> uuid.UUID | None:
    """Create or refresh the document for an incident / investigation.

    Returns the id when the content changed (needs indexing), otherwise None.
    """
    digest = content_hash(title, content)
    now = datetime.now(UTC)
    doc = await s.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.source == source, KnowledgeDocument.source_id == source_id
        )
    )
    if doc is None:
        doc = KnowledgeDocument(
            tenant_id=tenant_id,
            title=title[:300],
            document_type=source,
            source=source,
            source_id=source_id,
            content=content,
            content_hash=digest,
            resource_ids=resource_ids,
            created_by="system",
            source_synced_at=now,
        )
        s.add(doc)
        await s.flush()
        s.add(
            KnowledgeDocumentVersion(
                tenant_id=tenant_id, document_id=doc.id, version=1, title=doc.title,
                content=content, created_by="system",
            )
        )  # fmt: skip
        return doc.id
    doc.source_synced_at = now
    if doc.content_hash == digest and doc.resource_ids == resource_ids:
        return None
    doc.version += 1
    doc.title, doc.content, doc.content_hash = title[:300], content, digest
    doc.resource_ids = resource_ids
    doc.index_status = "pending"
    s.add(
        KnowledgeDocumentVersion(
            tenant_id=tenant_id, document_id=doc.id, version=doc.version, title=doc.title,
            content=content, created_by="system",
        )
    )  # fmt: skip
    return doc.id


async def sync_incident(s: AsyncSession, incident_id: uuid.UUID) -> uuid.UUID | None:
    inc = await s.get(Incident, incident_id)
    if inc is None or inc.status not in ("resolved", "closed"):
        return None
    resource_ids = list(
        await s.scalars(
            select(IncidentResource.resource_id).where(IncidentResource.incident_id == inc.id)
        )
    )
    return await _upsert_source_document(
        s,
        tenant_id=inc.tenant_id,
        source="incident",
        source_id=inc.id,
        title=f"{incident_key(inc.number)}: {inc.title}",
        content=_incident_markdown(inc),
        resource_ids=resource_ids,
    )


async def sync_investigation(s: AsyncSession, investigation_id: uuid.UUID) -> uuid.UUID | None:
    inv = await s.get(Investigation, investigation_id)
    if inv is None or inv.status != "completed" or not (inv.result or {}).get("root_cause"):
        return None
    resource_ids: list[uuid.UUID] = []
    for rid in inv.resource_ids or []:
        try:
            resource_ids.append(uuid.UUID(str(rid)))
        except ValueError:
            continue
    return await _upsert_source_document(
        s,
        tenant_id=inv.tenant_id,
        source="investigation",
        source_id=inv.id,
        title=f"Investigation: {inv.title}",
        content=_investigation_markdown(inv),
        resource_ids=resource_ids,
    )


async def knowledge_backlog() -> list[tuple[uuid.UUID, str, uuid.UUID]]:
    async with get_engine().connect() as conn:
        result = await conn.execute(
            text("SELECT tenant_id, kind, id FROM worker_knowledge_backlog()")
        )
        return [(r[0], r[1], r[2]) for r in result.all()]


async def sync_backlog(settings: Settings, *, client_factory: ClientFactory = ModelClient) -> int:
    """Copy new/changed incidents and investigations into the knowledge base and index
    them; also retries documents whose index job was lost. Returns documents indexed."""
    indexed = 0
    for tenant_id, kind, item_id in await knowledge_backlog():
        doc_id: uuid.UUID | None
        try:
            if kind == "document":
                doc_id = item_id
            else:
                async with tenant_session(tenant_id) as s:
                    doc_id = await (sync_incident if kind == "incident" else sync_investigation)(
                        s, item_id
                    )
            if doc_id is not None:
                await index_document(settings, tenant_id, doc_id, client_factory=client_factory)
                indexed += 1
        except Exception as exc:
            log.warning("knowledge_sync_failed", kind=kind, id=str(item_id), error=str(exc)[:300])
    if indexed:
        log.info("knowledge_synced", documents=indexed)
    return indexed


def snapshot(doc: KnowledgeDocument) -> dict[str, Any]:
    return {"id": str(doc.id), "title": doc.title, "version": doc.version}
