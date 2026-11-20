"""Phase 6 knowledge base: ingest, hybrid search, fallback, isolation, incident memory.

Qdrant runs in-process (``AsyncQdrantClient(":memory:")``) and the fake model server
answers ``/v1/embeddings`` with deterministic bag-of-words vectors.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from qdrant_client import AsyncQdrantClient, models

from app.core.config import get_settings
from app.knowledge import vectorstore
from app.knowledge.indexer import sync_backlog
from tests.fakes import FakeLLM
from tests.integration.conftest import Session, TenantInfo
from tests.integration.test_rca_scenarios import MODEL, _drain

pytestmark = pytest.mark.usefixtures("bootstrapped")

EMBEDDER = {
    "name": "Embeddings",
    "provider": "ollama",
    "purpose": "embedding",
    "base_url": "http://fake-llm:11434",
    "model_name": "nomic-embed-text",
    "is_default": True,
}

RUNBOOK = """# Payroll API runbook

## Connection pool exhausted

When payroll-api logs "too many clients already", the PostgreSQL connection pool is
exhausted. Restart the payroll-api container and raise max_connections on db-prod-01.

## Certificate expiry

Renew the TLS certificate with the internal CA before it expires.
"""


@pytest.fixture
async def qdrant() -> AsyncIterator[AsyncQdrantClient]:
    client = AsyncQdrantClient(":memory:")
    vectorstore.set_client_override(client)
    yield client
    vectorstore.set_client_override(None)
    await client.close()


def _pdf(text: str) -> bytes:
    """A minimal one-page PDF with a text layer."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


async def _search(s: Session, query: str, **kw: Any) -> dict[str, Any]:
    r = await s.post("/api/v1/knowledge/search", json={"query": query, **kw})
    assert r.status_code == 200, r.text
    return dict(r.json())


async def _runbook(app: FastAPI, s: Session, **kw: Any) -> dict[str, Any]:
    r = await s.post(
        "/api/v1/knowledge",
        json={"title": "Payroll API runbook", "content": RUNBOOK, "tags": ["payroll"], **kw},
    )
    assert r.status_code == 201, r.text
    await _drain(app)
    doc = (await s.get(f"/api/v1/knowledge/{r.json()['id']}")).json()
    return dict(doc)


async def test_runbooks_are_searchable_with_hybrid_search(
    app: FastAPI, make_tenant: Any, fake_llm: FakeLLM, qdrant: AsyncQdrantClient
) -> None:
    t: TenantInfo = await make_tenant()
    s = t.admin
    r = await s.post("/api/v1/models", json=EMBEDDER)
    assert r.status_code == 201, r.text
    test = (await s.post(f"/api/v1/models/{r.json()['id']}/test")).json()
    assert test["ok"] and test["dimensions"] == fake_llm.embed_dims

    doc = await _runbook(app, s)
    assert doc["index_status"] == "indexed", doc
    assert doc["embedding_model"] and doc["chunks"]

    body = await _search(s, "postgres pool exhausted too many clients")
    assert body["mode"] == "hybrid"
    top = body["hits"][0]
    assert top["document_id"] == doc["id"] and "too many clients" in top["excerpt"]
    assert top["match"] in ("both", "semantic", "keyword")
    # vectors alone find it too (no shared stemmed keyword with "clients already" text)
    assert any(h["match"] in ("both", "semantic") for h in body["hits"])

    # upload: Markdown and PDF
    files = {"file": ("disk.md", b"# Disk full\n\nClean /var/log with logrotate.", "text/markdown")}
    r = await s.post("/api/v1/knowledge/upload", files=files, data={"tags": "disk,linux"})
    assert r.status_code == 201, r.text
    pdf = _pdf("Rotate the Kafka broker keystore yearly")
    r = await s.post(
        "/api/v1/knowledge/upload",
        files={"file": ("kafka.pdf", pdf, "application/pdf")},
        data={"document_type": "procedure"},
    )
    assert r.status_code == 201, r.text
    pdf_id = r.json()["id"]
    await _drain(app)
    hits = (await _search(s, "kafka keystore"))["hits"]
    assert hits and hits[0]["document_id"] == pdf_id
    assert hits[0]["document_type"] == "procedure"
    bad = await s.post(
        "/api/v1/knowledge/upload", files={"file": ("x.exe", b"MZ\x00", "application/octet-stream")}
    )
    assert bad.status_code == 422

    # versioning: edit → v2, re-indexed; old version kept and restorable
    r = await s.put(
        f"/api/v1/knowledge/{doc['id']}",
        json={"content": RUNBOOK + "\n## Disk pressure\n\nExpand the volume.\n"},
    )
    assert r.status_code == 200 and r.json()["version"] == 2
    await _drain(app)
    versions = (await s.get(f"/api/v1/knowledge/{doc['id']}/versions")).json()
    assert [v["version"] for v in versions][:2] == [2, 1]
    assert (await _search(s, "expand the volume"))["hits"][0]["document_id"] == doc["id"]
    r = await s.post(f"/api/v1/knowledge/{doc['id']}/versions/1/restore")
    assert r.status_code == 200 and r.json()["version"] == 3
    await _drain(app)

    # validation boosts, deprecation hides
    assert (await s.post(f"/api/v1/knowledge/{doc['id']}/validate", json={})).status_code == 200
    r = await s.post(f"/api/v1/knowledge/{doc['id']}/feedback", json={"helpful": True})
    assert r.status_code == 204
    hit = (await _search(s, "connection pool"))["hits"][0]
    assert hit["validated"] is True
    r = await s.put(f"/api/v1/knowledge/{doc['id']}", json={"status": "deprecated"})
    assert r.status_code == 200
    await _drain(app)
    ids = {h["document_id"] for h in (await _search(s, "connection pool"))["hits"]}
    assert doc["id"] not in ids
    ids = {
        h["document_id"]
        for h in (await _search(s, "connection pool", include_deprecated=True))["hits"]
    }
    assert doc["id"] in ids

    stats = (await s.get("/api/v1/knowledge/stats")).json()
    assert stats["documents"] >= 3 and stats["vector_store"] == "ok"

    # delete removes the vectors too
    assert (await s.delete(f"/api/v1/knowledge/{pdf_id}")).status_code == 204
    assert (await s.get(f"/api/v1/knowledge/{pdf_id}")).status_code == 404
    assert pdf_id not in {h["document_id"] for h in (await _search(s, "kafka keystore"))["hits"]}


async def test_keyword_fallback_without_embedding_model(
    app: FastAPI, make_tenant: Any, fake_llm: FakeLLM, qdrant: AsyncQdrantClient
) -> None:
    t: TenantInfo = await make_tenant()
    s = t.admin
    doc = await _runbook(app, s)
    assert doc["index_status"] == "keyword_only"
    body = await _search(s, "connection pool exhausted")
    assert body["mode"] == "keyword" and body["note"]
    assert body["hits"][0]["document_id"] == doc["id"]
    assert body["hits"][0]["match"] == "keyword"

    # an embedding endpoint that is down also degrades to keyword search
    assert (await s.post("/api/v1/models", json=EMBEDDER)).status_code == 201
    fake_llm.embed_fail_status = 503
    doc = await _runbook(app, s, title="Second runbook")
    assert doc["index_status"] == "keyword_only" and doc["index_error"]
    assert (await _search(s, "certificate expiry"))["hits"]


async def test_tenant_isolation_in_keyword_and_vector_search(
    app: FastAPI, make_tenant: Any, fake_llm: FakeLLM, qdrant: AsyncQdrantClient
) -> None:
    a: TenantInfo = await make_tenant()
    b: TenantInfo = await make_tenant()
    for t in (a, b):
        assert (await t.admin.post("/api/v1/models", json=EMBEDDER)).status_code == 201
    doc = await _runbook(app, a.admin)
    assert doc["index_status"] == "indexed"

    assert not (await _search(b.admin, "connection pool exhausted"))["hits"]
    assert (await b.admin.get(f"/api/v1/knowledge/{doc['id']}")).status_code == 404
    assert (await b.admin.get("/api/v1/knowledge")).json()["total"] == 0

    # Forge a point in B's tenant space that points at A's chunk: rehydration under RLS
    # must still drop it.
    collections = (await qdrant.get_collections()).collections
    name = collections[0].name
    chunk = doc["chunks"][0]
    await qdrant.upsert(
        name,
        points=[
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector=fake_llm.embedding("connection pool exhausted"),
                payload={
                    "tenant_id": b.id,
                    "document_id": doc["id"],
                    "chunk_id": chunk["id"],
                    "document_type": "runbook",
                    "status": "active",
                    "resource_ids": [],
                },
            )
        ],
    )
    assert not (await _search(b.admin, "connection pool exhausted"))["hits"]


async def test_resolved_incidents_become_searchable_memory(
    app: FastAPI, make_tenant: Any, fake_llm: FakeLLM, qdrant: AsyncQdrantClient
) -> None:
    t: TenantInfo = await make_tenant()
    s = t.admin
    assert (await s.post("/api/v1/models", json=MODEL)).status_code == 201
    assert (await s.post("/api/v1/models", json=EMBEDDER)).status_code == 201
    r = await s.post(
        "/api/v1/incidents",
        json={
            "title": "Payroll API returns 500",
            "description": "PostgreSQL refused connections: too many clients already",
            "severity": "high",
        },
    )
    old = r.json()["id"]
    r = await s.post(
        f"/api/v1/incidents/{old}/close",
        json={"resolution": "Raised max_connections on db-prod-01 and restarted payroll-api"},
    )
    assert r.status_code == 200, r.text
    assert await sync_backlog(get_settings()) >= 1
    assert await sync_backlog(get_settings()) == 0  # unchanged content is not re-indexed

    docs = (await s.get("/api/v1/knowledge", params={"document_type": "incident"})).json()
    assert docs["total"] == 1 and docs["items"][0]["source_id"] == old
    hits = (await _search(s, "payroll database connections refused"))["hits"]
    assert hits and hits[0]["source"] == "incident"
    # generated documents are read-only apart from status, tags and category
    doc_id = docs["items"][0]["id"]
    assert (await s.put(f"/api/v1/knowledge/{doc_id}", json={"content": "x"})).status_code in (
        409,
        422,
    )

    r = await s.post(
        "/api/v1/incidents",
        json={
            "title": "Payroll errors again",
            "description": "payroll-api cannot get a database connection",
            "severity": "medium",
        },
    )
    similar = (await s.get(f"/api/v1/incidents/{r.json()['id']}/similar")).json()
    assert similar and similar[0]["id"] == old
    assert similar[0]["match"] in ("keyword", "semantic", "both")
    assert "max_connections" in (similar[0]["resolution"] or "")


async def test_knowledge_rbac(app: FastAPI, make_tenant: Any, login: Any) -> None:
    from tests.integration.test_remediation import _user

    t: TenantInfo = await make_tenant()
    viewer = await _user(t, login, "viewer1", "Read Only")
    assert (await viewer.get("/api/v1/knowledge")).status_code == 200
    r = await viewer.post("/api/v1/knowledge", json={"title": "x", "content": "y"})
    assert r.status_code == 403
