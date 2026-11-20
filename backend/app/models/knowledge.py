"""Knowledge base: documents, versions, chunks and feedback (phase 6).

PostgreSQL is authoritative (metadata, content, full-text vectors); Qdrant only holds
the embeddings of ``knowledge_chunks`` keyed by chunk id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, CreatedAtMixin, UUIDPkMixin

DOCUMENT_TYPES = (
    "runbook",
    "troubleshooting",
    "documentation",
    "procedure",
    "incident",
    "investigation",
    "other",
)
DOCUMENT_SOURCES = ("upload", "manual", "incident", "investigation")
DOCUMENT_STATUSES = ("active", "draft", "deprecated")
INDEX_STATUSES = ("pending", "indexed", "keyword_only", "failed")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class KnowledgeDocument(UUIDPkMixin, CreatedAtMixin, Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_knowledge_documents_id_tenant"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        CheckConstraint(f"document_type IN {DOCUMENT_TYPES!r}", name="type"),
        CheckConstraint(f"source IN {DOCUMENT_SOURCES!r}", name="source"),
        CheckConstraint(f"status IN {DOCUMENT_STATUSES!r}", name="status"),
        CheckConstraint(f"index_status IN {INDEX_STATUSES!r}", name="index_status"),
        # One knowledge document per incident / investigation.
        Index(
            "uq_knowledge_documents_source",
            "tenant_id",
            "source",
            "source_id",
            unique=True,
            postgresql_where="source_id IS NOT NULL",
        ),
        Index("ix_knowledge_documents_tenant_type", "tenant_id", "document_type"),
        Index("ix_knowledge_documents_search", "search_vector", postgresql_using="gin"),
        Index("ix_knowledge_documents_resources", "resource_ids", postgresql_using="gin"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    document_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, server_default="upload")
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}", default=dict
    )
    category: Mapped[str | None] = mapped_column(String(100))
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(60)), nullable=False, server_default="{}", default=list
    )
    resource_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default="{}", default=list
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    validated_by: Mapped[str | None] = mapped_column(String(100))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    index_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending", default="pending"
    )
    index_error: Mapped[str | None] = mapped_column(String(1000))
    indexed_hash: Mapped[str | None] = mapped_column(String(64))
    embedding_model: Mapped[str | None] = mapped_column(String(200))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Last time an incident / investigation source was copied into this document.
    source_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=_utcnow)
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(content, '')), 'B')",
            persisted=True,
        ),
    )


class KnowledgeDocumentVersion(UUIDPkMixin, CreatedAtMixin, Base):
    __tablename__ = "knowledge_document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["knowledge_documents.id", "knowledge_documents.tenant_id"],
            ondelete="CASCADE",
            name="fk_knowledge_versions_document_tenant",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(String(300))


class KnowledgeChunk(UUIDPkMixin, Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["knowledge_documents.id", "knowledge_documents.tenant_id"],
            ondelete="CASCADE",
            name="fk_knowledge_chunks_document_tenant",
        ),
        Index("ix_knowledge_chunks_search", "search_vector", postgresql_using="gin"),
        Index("ix_knowledge_chunks_tenant", "tenant_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_id: Mapped[str | None] = mapped_column(String(100))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}", default=dict
    )
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(metadata ->> 'heading', '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(content, '')), 'B')",
            persisted=True,
        ),
    )


class KnowledgeFeedback(UUIDPkMixin, CreatedAtMixin, Base):
    __tablename__ = "knowledge_feedback"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["knowledge_documents.id", "knowledge_documents.tenant_id"],
            ondelete="CASCADE",
            name="fk_knowledge_feedback_document_tenant",
        ),
        Index("ix_knowledge_feedback_document", "document_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    username: Mapped[str | None] = mapped_column(String(100))
    helpful: Mapped[bool] = mapped_column(Boolean, nullable=False)
    comment: Mapped[str | None] = mapped_column(String(1000))
