"""Hybrid knowledge search: PostgreSQL full-text + Qdrant vectors, fused with RRF.

Keyword search always works (it is plain SQL under RLS). Semantic search needs an
embedding endpoint and Qdrant; when either is missing the result says so and the
keyword ranking is used alone. Both lists are fused with Reciprocal Rank Fusion, then
gently boosted for validated documents, matching resources and helpful feedback.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.knowledge import vectorstore
from app.llm.gateway import EndpointConfig, ModelClient, ModelError
from app.models import KnowledgeChunk, KnowledgeDocument, KnowledgeFeedback
from app.services.knowledge_search import query_terms
from app.services.model_service import default_embedding_endpoint

log = get_logger("localops.knowledge.search")

ClientFactory = Callable[[EndpointConfig], ModelClient]
RRF_K = 60
CANDIDATES = 40


@dataclass(slots=True)
class SearchHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    title: str
    document_type: str
    heading: str
    excerpt: str
    content: str
    score: float
    match: str  # keyword | semantic | both
    version: int
    validated: bool
    status: str
    tags: list[str]
    resource_ids: list[uuid.UUID]
    source: str
    source_id: uuid.UUID | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": str(self.chunk_id),
            "document_id": str(self.document_id),
            "title": self.title,
            "document_type": self.document_type,
            "heading": self.heading,
            "excerpt": self.excerpt,
            "score": round(self.score, 4),
            "match": self.match,
            "version": self.version,
            "validated": self.validated,
            "status": self.status,
            "tags": self.tags,
            "resource_ids": [str(r) for r in self.resource_ids],
            "source": self.source,
            "source_id": str(self.source_id) if self.source_id else None,
        }


@dataclass(slots=True)
class SearchResult:
    hits: list[SearchHit] = field(default_factory=list)
    mode: str = "keyword"  # hybrid | keyword
    note: str | None = None


def _tsquery(terms: list[str]) -> Any:
    safe = [re.sub(r"[^a-z0-9_]", "", t) for t in terms]
    return func.to_tsquery("english", " | ".join(t for t in safe if t) or "none")


def excerpt(content: str, terms: list[str], size: int = 360) -> str:
    text = " ".join(content.split())
    lowered = text.lower()
    positions = [p for p in (lowered.find(t) for t in terms) if p >= 0]
    start = max(min(positions) - size // 4, 0) if positions else 0
    snippet = text[start : start + size]
    return ("…" if start else "") + snippet + ("…" if start + size < len(text) else "")


@dataclass(frozen=True, slots=True)
class _Filters:
    document_types: list[str] | None
    resource_id: uuid.UUID | None
    tags: list[str] | None
    include_deprecated: bool
    exclude_sources: list[uuid.UUID] | None

    def apply(self, stmt: Any) -> Any:  # Select of any shape
        if self.document_types:
            stmt = stmt.where(KnowledgeDocument.document_type.in_(self.document_types))
        if self.resource_id is not None:
            stmt = stmt.where(KnowledgeDocument.resource_ids.contains([self.resource_id]))
        if self.tags:
            stmt = stmt.where(KnowledgeDocument.tags.overlap(self.tags))
        if not self.include_deprecated:
            stmt = stmt.where(KnowledgeDocument.status != "deprecated")
        if self.exclude_sources:
            stmt = stmt.where(
                (KnowledgeDocument.source_id.is_(None))
                | (KnowledgeDocument.source_id.not_in(self.exclude_sources))
            )
        return stmt


async def hybrid_search(
    session: AsyncSession,
    settings: Settings,
    tenant_id: uuid.UUID,
    query: str,
    *,
    document_types: list[str] | None = None,
    resource_id: uuid.UUID | None = None,
    boost_resources: list[uuid.UUID] | None = None,
    tags: list[str] | None = None,
    include_deprecated: bool = False,
    exclude_sources: list[uuid.UUID] | None = None,
    limit: int = 10,
    per_document: int = 2,
    semantic: bool = True,
    client_factory: ClientFactory = ModelClient,
) -> SearchResult:
    """Search chunks visible to the session's tenant (RLS) — see module docstring."""
    query = " ".join(query.split())[:2000]
    result = SearchResult()
    if not query:
        return result
    terms = query_terms(query, limit=16)
    filters = _Filters(document_types, resource_id, tags, include_deprecated, exclude_sources)

    # keyword candidates
    keyword_ranks: dict[uuid.UUID, int] = {}
    if terms:
        tsq = _tsquery(terms)
        rank = func.ts_rank_cd(KnowledgeChunk.search_vector, tsq)
        stmt = (
            select(KnowledgeChunk.id)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .where(KnowledgeChunk.search_vector.op("@@")(tsq))
            .order_by(rank.desc())
            .limit(CANDIDATES)
        )
        ids = list(await session.scalars(filters.apply(stmt)))
        keyword_ranks = {cid: i for i, cid in enumerate(ids)}

    # semantic candidates
    semantic_ranks: dict[uuid.UUID, int] = {}
    semantic_scores: dict[uuid.UUID, float] = {}
    endpoint = await default_embedding_endpoint(session, settings) if semantic else None
    if not semantic:
        result.note = None
    elif endpoint is None:
        result.note = "No embedding model configured: keyword search only"
    else:
        config, fingerprint = endpoint
        try:
            vectors = await client_factory(config).embed([query])
            name = vectorstore.collection_name(settings, fingerprint, len(vectors[0]))
            hits = await vectorstore.search(
                settings,
                name,
                tenant_id,
                vectors[0],
                limit=CANDIDATES,
                document_types=document_types,
                resource_id=resource_id,
            )
            semantic_ranks = {h.chunk_id: i for i, h in enumerate(hits)}
            semantic_scores = {h.chunk_id: h.score for h in hits}
            result.mode = "hybrid"
        except ModelError as exc:
            result.note = f"Semantic search unavailable ({exc}); keyword results only"
        except Exception as exc:
            result.note = "Vector store unavailable; keyword results only"
            log.warning("vector_search_failed", error=str(exc)[:300])

    candidates = set(keyword_ranks) | set(semantic_ranks)
    if not candidates:
        return result

    # rehydrate under RLS (drops anything not visible to this tenant) and re-filter
    rows_stmt = (
        select(KnowledgeChunk, KnowledgeDocument)
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .where(KnowledgeChunk.id.in_(candidates))
    )
    rows = (await session.execute(filters.apply(rows_stmt))).all()
    doc_ids = {d.id for _, d in rows}
    feedback: dict[uuid.UUID, int] = {}
    if doc_ids:
        fb = await session.execute(
            select(
                KnowledgeFeedback.document_id,
                func.sum(case((KnowledgeFeedback.helpful.is_(True), 1), else_=-1)),
            )
            .where(KnowledgeFeedback.document_id.in_(doc_ids))
            .group_by(KnowledgeFeedback.document_id)
        )
        feedback = {d: int(n or 0) for d, n in fb.all()}
    boost_set = set(boost_resources or [])

    scored: list[SearchHit] = []
    for chunk, doc in rows:
        k, v = keyword_ranks.get(chunk.id), semantic_ranks.get(chunk.id)
        score = (1 / (RRF_K + k) if k is not None else 0) + (
            1 / (RRF_K + v) if v is not None else 0
        )
        if doc.validated_at:
            score *= 1.2
        if boost_set and boost_set & set(doc.resource_ids):
            score *= 1.3
        net = feedback.get(doc.id, 0)
        score *= 1.1 if net > 0 else 0.85 if net < 0 else 1.0
        if doc.status == "draft":
            score *= 0.8
        match = (
            "both"
            if k is not None and v is not None
            else "keyword"
            if k is not None
            else "semantic"
        )
        scored.append(
            SearchHit(
                chunk_id=chunk.id,
                document_id=doc.id,
                title=doc.title,
                document_type=doc.document_type,
                heading=(chunk.metadata_ or {}).get("heading", ""),
                excerpt=excerpt(chunk.content, terms),
                content=chunk.content,
                score=score,
                match=match,
                version=doc.version,
                validated=doc.validated_at is not None,
                status=doc.status,
                tags=list(doc.tags or []),
                resource_ids=list(doc.resource_ids or []),
                source=doc.source,
                source_id=doc.source_id,
            )
        )
    scored.sort(key=lambda h: (-h.score, -semantic_scores.get(h.chunk_id, 0.0)))
    per_doc: dict[uuid.UUID, int] = {}
    for hit in scored:
        if per_doc.get(hit.document_id, 0) >= per_document:
            continue
        per_doc[hit.document_id] = per_doc.get(hit.document_id, 0) + 1
        result.hits.append(hit)
        if len(result.hits) >= limit:
            break
    return result
