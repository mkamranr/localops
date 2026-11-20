"""Qdrant access for knowledge chunk embeddings — always scoped to one tenant.

Every function takes ``tenant_id`` and applies it as a mandatory payload filter (writes
stamp it into the payload). Callers then rehydrate hits from PostgreSQL under RLS, so a
foreign id that somehow slipped through is dropped a second time.

One collection per embedding model (``<prefix>_<fingerprint>``): changing the model
never mixes vectors of different spaces; re-indexing fills the new collection.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger("localops.knowledge.vectors")

_client: AsyncQdrantClient | None = None
_override: AsyncQdrantClient | None = None
_known: set[str] = set()


def set_client_override(client: AsyncQdrantClient | None) -> None:
    """Tests inject an in-memory client (``AsyncQdrantClient(":memory:")``)."""
    global _override
    _override = client
    _known.clear()


def client(settings: Settings) -> AsyncQdrantClient:
    global _client
    if _override is not None:
        return _override
    if _client is None:
        _client = AsyncQdrantClient(url=settings.qdrant_url, timeout=10, check_compatibility=False)
    return _client


def collection_name(settings: Settings, fingerprint: str, dimensions: int) -> str:
    digest = hashlib.sha256(f"{fingerprint}|{dimensions}".encode()).hexdigest()[:12]
    return f"{settings.knowledge_vector_collection_prefix}_{digest}"


@dataclass(frozen=True, slots=True)
class VectorHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    score: float


def _tenant_filter(tenant_id: uuid.UUID, extra: list[Any] | None = None) -> models.Filter:
    must: list[Any] = [
        models.FieldCondition(key="tenant_id", match=models.MatchValue(value=str(tenant_id)))
    ]
    return models.Filter(must=must + (extra or []))


async def ensure_collection(settings: Settings, name: str, dimensions: int) -> None:
    if name in _known:
        return
    qc = client(settings)
    if not await qc.collection_exists(name):
        await qc.create_collection(
            name,
            vectors_config=models.VectorParams(size=dimensions, distance=models.Distance.COSINE),
        )
        for field, schema in (
            (
                "tenant_id",
                models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD, is_tenant=True),
            ),
            ("document_id", models.PayloadSchemaType.KEYWORD),
            ("document_type", models.PayloadSchemaType.KEYWORD),
            ("resource_ids", models.PayloadSchemaType.KEYWORD),
        ):
            await qc.create_payload_index(name, field, field_schema=schema)
        log.info("vector_collection_created", collection=name, dimensions=dimensions)
    _known.add(name)


async def upsert_chunks(
    settings: Settings,
    name: str,
    tenant_id: uuid.UUID,
    points: list[tuple[uuid.UUID, list[float], dict[str, Any]]],
) -> None:
    """Replace vectors of the given chunks; the tenant id is stamped, never trusted."""
    if not points:
        return
    await client(settings).upsert(
        name,
        points=[
            models.PointStruct(
                id=str(chunk_id),
                vector=vector,
                payload={**payload, "tenant_id": str(tenant_id), "chunk_id": str(chunk_id)},
            )
            for chunk_id, vector, payload in points
        ],
    )


async def delete_document(settings: Settings, tenant_id: uuid.UUID, document_id: uuid.UUID) -> None:
    """Drop a document's vectors from every knowledge collection."""
    qc = client(settings)
    selector = models.FilterSelector(
        filter=_tenant_filter(
            tenant_id,
            [
                models.FieldCondition(
                    key="document_id", match=models.MatchValue(value=str(document_id))
                )
            ],
        )
    )
    for collection in (await qc.get_collections()).collections:
        if collection.name.startswith(settings.knowledge_vector_collection_prefix + "_"):
            await qc.delete(collection.name, points_selector=selector)


async def search(
    settings: Settings,
    name: str,
    tenant_id: uuid.UUID,
    vector: list[float],
    *,
    limit: int = 20,
    document_types: list[str] | None = None,
    resource_id: uuid.UUID | None = None,
) -> list[VectorHit]:
    qc = client(settings)
    if not await qc.collection_exists(name):
        return []
    extra: list[Any] = []
    if document_types:
        extra.append(
            models.FieldCondition(key="document_type", match=models.MatchAny(any=document_types))
        )
    if resource_id is not None:
        extra.append(
            models.FieldCondition(
                key="resource_ids", match=models.MatchValue(value=str(resource_id))
            )
        )
    result = await qc.query_points(
        name,
        query=vector,
        query_filter=_tenant_filter(tenant_id, extra),
        limit=limit,
        with_payload=["tenant_id", "document_id"],
    )
    hits: list[VectorHit] = []
    for point in result.points:
        payload = point.payload or {}
        if payload.get("tenant_id") != str(tenant_id):  # belt and braces
            continue
        try:
            hits.append(
                VectorHit(
                    uuid.UUID(str(point.id)), uuid.UUID(str(payload["document_id"])), point.score
                )
            )
        except (KeyError, ValueError):
            continue
    return hits


async def healthy(settings: Settings) -> bool:
    try:
        await client(settings).get_collections()
        return True
    except Exception:
        return False
