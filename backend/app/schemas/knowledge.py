"""Knowledge base API schemas (spec §10.9)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ApiModel

DocumentType = Literal[
    "runbook", "troubleshooting", "documentation", "procedure", "incident", "investigation",
    "other",
]  # fmt: skip
EditableType = Literal["runbook", "troubleshooting", "documentation", "procedure", "other"]
DocumentStatus = Literal["active", "draft", "deprecated"]
IndexStatus = Literal["pending", "indexed", "keyword_only", "failed"]


def _clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for tag in tags:
        tag = " ".join(str(tag).split()).lower()[:60]
        if tag and tag not in out:
            out.append(tag)
    return out[:20]


class DocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    document_type: EditableType = "runbook"
    content: str = Field(min_length=1, max_length=2_000_000)
    category: str | None = Field(None, max_length=100)
    tags: list[str] = Field(default_factory=list)
    resource_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    status: Literal["active", "draft"] = "active"

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return _clean_tags(v)


class DocumentUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=300)
    document_type: EditableType | None = None
    content: str | None = Field(None, min_length=1, max_length=2_000_000)
    category: str | None = Field(None, max_length=100)
    tags: list[str] | None = None
    resource_ids: list[uuid.UUID] | None = Field(None, max_length=50)
    status: DocumentStatus | None = None
    note: str | None = Field(None, max_length=300, description="Change note for the version")

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str] | None) -> list[str] | None:
        return _clean_tags(v) if v is not None else v


class DocumentOut(ApiModel):
    id: uuid.UUID
    title: str
    document_type: str
    source: str
    source_id: uuid.UUID | None
    category: str | None
    tags: list[str]
    resource_ids: list[uuid.UUID]
    status: str
    version: int
    validated_by: str | None
    validated_at: datetime | None
    index_status: str
    index_error: str | None
    embedding_model: str | None
    indexed_at: datetime | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime | None
    size: int = 0
    filename: str | None = None
    chunk_count: int = 0
    helpful: int = 0
    not_helpful: int = 0


class ChunkOut(BaseModel):
    id: uuid.UUID
    chunk_index: int
    heading: str
    content: str
    embedded: bool


class DocumentDetail(DocumentOut):
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunks: list[ChunkOut] = Field(default_factory=list)
    resource_names: dict[str, str] = Field(default_factory=dict)


class VersionOut(ApiModel):
    version: int
    title: str
    created_by: str | None
    created_at: datetime
    note: str | None
    size: int = 0


class VersionDetail(VersionOut):
    content: str


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    document_types: list[DocumentType] | None = None
    resource_id: uuid.UUID | None = None
    tags: list[str] | None = None
    include_deprecated: bool = False
    limit: int = Field(10, ge=1, le=50)


class SearchHitOut(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    title: str
    document_type: str
    heading: str
    excerpt: str
    score: float
    match: Literal["keyword", "semantic", "both"]
    version: int
    validated: bool
    status: str
    tags: list[str]
    resource_ids: list[uuid.UUID]
    source: str
    source_id: uuid.UUID | None


class SearchResponse(BaseModel):
    mode: Literal["hybrid", "keyword"]
    note: str | None = None
    hits: list[SearchHitOut]


class FeedbackIn(BaseModel):
    helpful: bool
    comment: str | None = Field(None, max_length=1000)
    chunk_id: uuid.UUID | None = None
    investigation_id: uuid.UUID | None = None


class ValidateIn(BaseModel):
    validated: bool = True


class ReindexRequest(BaseModel):
    document_ids: list[uuid.UUID] | None = Field(
        None, description="Documents to re-index; omit for every document", max_length=1000
    )


class ReindexResult(BaseModel):
    queued: int


class KnowledgeStats(BaseModel):
    documents: int
    chunks: int
    by_type: dict[str, int]
    by_status: dict[str, int]
    by_index_status: dict[str, int]
    validated: int
    embedding_model: str | None
    vector_store: Literal["ok", "unavailable", "not_configured"]
