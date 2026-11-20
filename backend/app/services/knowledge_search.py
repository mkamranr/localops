"""Similar incidents and investigations: full-text + semantic, fused.

Ranking combines text relevance (``ts_rank`` over the incident search vector, or the
investigation request/title), overlap of affected resources and — when an embedding
model is configured — semantic similarity of the knowledge documents generated from
resolved incidents and completed investigations (``app.knowledge``). Runs under the
tenant's RLS like every other query; without embeddings it is plain full-text search.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy import text as sql_text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import Incident, IncidentResource, Investigation, incident_key

log = get_logger("localops.knowledge.similar")
SEMANTIC_WEIGHT = 0.6

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,40}")
_STOP = {
    "the", "and", "for", "why", "is", "are", "was", "with", "from", "that", "this", "has",
    "have", "not", "since", "again", "keeps", "what", "how", "when", "does", "did", "on",
    "in", "of", "to", "a", "an", "my", "our", "it", "be",
}  # fmt: skip


def query_terms(text: str, limit: int = 12) -> list[str]:
    seen: list[str] = []
    for word in _WORD.findall(text.lower()):
        if word not in _STOP and word not in seen:
            seen.append(word)
    return seen[:limit]


def _tsquery(terms: list[str]) -> Any:
    # OR-query of plain terms; to_tsquery input is built from validated words only.
    safe = [re.sub(r"[^a-z0-9_]", "", t) for t in terms]
    return func.to_tsquery("english", " | ".join(t for t in safe if t) or "none")


async def semantic_sources(
    session: AsyncSession,
    query: str,
    source: str,
    *,
    exclude_id: uuid.UUID | None = None,
    limit: int = 10,
) -> dict[uuid.UUID, float]:
    """Source ids (incident / investigation) ranked by the knowledge index.

    Returns {source_id: weight in (0, 1]} — empty when no embedding model is
    configured or the vector store is unavailable (keyword ranking then stands alone).
    """
    from app.core.config import get_settings
    from app.knowledge.search import hybrid_search

    tenant = await session.scalar(sql_text("SELECT app_current_tenant()"))
    if tenant is None or not query.strip():
        return {}
    try:
        result = await hybrid_search(
            session,
            get_settings(),
            tenant,
            query,
            document_types=[source],
            exclude_sources=[exclude_id] if exclude_id else None,
            limit=limit,
            per_document=1,
        )
    except Exception as exc:  # degrade to keyword ranking
        log.warning("semantic_similar_failed", error=str(exc)[:200])
        return {}
    if result.mode != "hybrid":
        return {}
    out: dict[uuid.UUID, float] = {}
    for rank, hit in enumerate(h for h in result.hits if h.match in ("semantic", "both")):
        if hit.source_id is not None and hit.source_id not in out:
            out[hit.source_id] = 1 / (1 + rank * 0.5)
    return out


def _match(keyword: bool, semantic: bool) -> str:
    return "both" if keyword and semantic else "semantic" if semantic else "keyword"


async def similar_incidents(
    session: AsyncSession,
    text: str,
    resource_ids: list[uuid.UUID] | None = None,
    *,
    exclude_id: uuid.UUID | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    terms = query_terms(text)
    resource_ids = resource_ids or []
    query = _tsquery(terms)
    rank = func.ts_rank(Incident.search_vector, query)
    overlap = (
        select(func.count())
        .select_from(IncidentResource)
        .where(
            IncidentResource.incident_id == Incident.id,
            IncidentResource.resource_id.in_(resource_ids or [uuid.uuid4()]),
        )
        .scalar_subquery()
    )
    score = (rank + overlap * literal(0.5)).label("score")
    stmt = (
        select(Incident, score)
        .where(or_(Incident.search_vector.op("@@")(query), overlap > 0))
        .order_by(score.desc(), Incident.created_at.desc())
        .limit(limit * 2)
    )
    if exclude_id is not None:
        stmt = stmt.where(Incident.id != exclude_id)
    rows = [(i, float(sc)) for i, sc in (await session.execute(stmt)).all()]
    semantic = await semantic_sources(session, text, "incident", exclude_id=exclude_id)
    keyword_ids = {i.id for i, _ in rows}
    missing = [sid for sid in semantic if sid not in keyword_ids]
    if missing:
        rows += [
            (i, 0.0)
            for i in await session.scalars(select(Incident).where(Incident.id.in_(missing)))
        ]
    scored = sorted(
        ((i, sc + SEMANTIC_WEIGHT * semantic.get(i.id, 0.0)) for i, sc in rows),
        key=lambda pair: (-pair[1], -pair[0].created_at.timestamp()),
    )[:limit]
    return [
        {
            "kind": "incident",
            "id": str(i.id),
            "number": incident_key(i.number),
            "title": i.title,
            "status": i.status,
            "severity": i.severity,
            "root_cause": (i.root_cause or {}).get("statement"),
            "resolution": i.resolution,
            "created_at": i.created_at.isoformat(),
            "score": round(sc, 3),
            "match": _match(i.id in keyword_ids, i.id in semantic),
        }
        for i, sc in scored
    ]


async def similar_investigations(
    session: AsyncSession,
    text: str,
    resource_ids: list[uuid.UUID] | None = None,
    *,
    exclude_id: uuid.UUID | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    terms = query_terms(text)
    query = _tsquery(terms)
    doc = func.to_tsvector(
        "english", func.coalesce(Investigation.title, "") + " " + Investigation.request
    )
    ids = [str(r) for r in resource_ids or []]
    overlap: Any = (
        case((Investigation.resource_ids.op("?|")(postgresql.array(ids)), 1), else_=0)
        if ids
        else literal(0)
    )
    score = (func.ts_rank(doc, query) + overlap * literal(0.5)).label("score")
    stmt = (
        select(Investigation, score)
        .where(Investigation.status == "completed")
        .where(or_(doc.op("@@")(query), overlap > 0))
        .order_by(score.desc(), Investigation.started_at.desc())
        .limit(limit * 2)
    )
    if exclude_id is not None:
        stmt = stmt.where(Investigation.id != exclude_id)
    rows = [(v, float(sc)) for v, sc in (await session.execute(stmt)).all()]
    semantic = await semantic_sources(session, text, "investigation", exclude_id=exclude_id)
    keyword_ids = {v.id for v, _ in rows}
    missing = [sid for sid in semantic if sid not in keyword_ids]
    if missing:
        rows += [
            (v, 0.0)
            for v in await session.scalars(
                select(Investigation).where(
                    Investigation.id.in_(missing), Investigation.status == "completed"
                )
            )
        ]
    scored = sorted(
        ((v, sc + SEMANTIC_WEIGHT * semantic.get(v.id, 0.0)) for v, sc in rows),
        key=lambda pair: (-pair[1], -pair[0].started_at.timestamp()),
    )[:limit]
    out = []
    for inv, s in scored:
        root = (inv.result or {}).get("root_cause") or {}
        out.append(
            {
                "kind": "investigation",
                "id": str(inv.id),
                "title": inv.title,
                "status": inv.status,
                "root_cause": root.get("statement"),
                "conclusion": (inv.result or {}).get("conclusion"),
                "created_at": inv.started_at.isoformat(),
                "score": round(float(s), 3),
                "match": _match(inv.id in keyword_ids, inv.id in semantic),
            }
        )
    return out
