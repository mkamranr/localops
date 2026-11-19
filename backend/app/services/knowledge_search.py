"""Similar incidents and investigations via PostgreSQL full-text search.

Ranking combines text relevance (``ts_rank`` over the incident search vector, or the
investigation request/title) with overlap of affected resources. Runs under the
tenant's RLS like every other query. Vector search (Qdrant) arrives with the
Phase 6 knowledge base.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Incident, IncidentResource, Investigation, incident_key

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
        .limit(limit)
    )
    if exclude_id is not None:
        stmt = stmt.where(Incident.id != exclude_id)
    rows = (await session.execute(stmt)).all()
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
            "score": round(float(s), 3),
        }
        for i, s in rows
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
        .limit(limit)
    )
    if exclude_id is not None:
        stmt = stmt.where(Investigation.id != exclude_id)
    rows = (await session.execute(stmt)).all()
    out = []
    for inv, s in rows:
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
            }
        )
    return out
