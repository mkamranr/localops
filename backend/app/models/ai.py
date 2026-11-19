"""Model endpoints, agents, agent executions, investigations and their events."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, CreatedAtMixin, UUIDPkMixin

PROVIDERS = ("vllm", "ollama", "llamacpp", "openai_compatible")
INVESTIGATION_STATUSES = (
    "queued",
    "running",
    "awaiting_input",
    "awaiting_approval",
    "completed",
    "failed",
    "cancelled",
)
ACTIVE_STATUSES = ("queued", "running", "awaiting_input", "awaiting_approval")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ModelEndpoint(UUIDPkMixin, CreatedAtMixin, Base):
    __tablename__ = "model_endpoints"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name"),
        UniqueConstraint("id", "tenant_id", name="uq_model_endpoints_id_tenant"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        CheckConstraint(f"provider IN {PROVIDERS!r}", name="provider"),
        # At most one default model per tenant.
        Index(
            "uq_model_endpoints_default",
            "tenant_id",
            unique=True,
            postgresql_where="is_default",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    encrypted_api_key: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(20))
    last_latency_ms: Mapped[float | None] = mapped_column(Float)
    last_error: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=_utcnow)


class Agent(UUIDPkMixin, CreatedAtMixin, Base):
    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_type"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        # A tenant's agent can only use that tenant's model endpoints.
        ForeignKeyConstraint(
            ["model_id", "tenant_id"],
            ["model_endpoints.id", "model_endpoints.tenant_id"],
            name="fk_agents_model_tenant",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_type: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=_utcnow)


class AgentExecution(UUIDPkMixin, Base):
    __tablename__ = "agent_executions"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        Index("ix_agent_executions_tenant_started", "tenant_id", "started_at"),
        Index("ix_agent_executions_agent", "tenant_id", "agent_type", "started_at"),
        Index("ix_agent_executions_investigation", "investigation_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_type: Mapped[str] = mapped_column(String(50), nullable=False)
    node: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # running|success|failed
    model_name: Mapped[str | None] = mapped_column(String(200))
    model_endpoint_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    error: Mapped[str | None] = mapped_column(String(1000))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class Investigation(UUIDPkMixin, Base):
    __tablename__ = "investigations"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_investigations_id_tenant"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_investigations_user_tenant",
        ),
        ForeignKeyConstraint(
            ["incident_id", "tenant_id"],
            ["incidents.id", "incidents.tenant_id"],
            name="fk_investigations_incident_tenant",
        ),
        Index("ix_investigations_incident", "incident_id"),
        CheckConstraint(f"status IN {INVESTIGATION_STATUSES!r}", name="status"),
        CheckConstraint("execution_mode IN ('manual','assisted','autonomous')", name="mode"),
        Index("ix_investigations_tenant_started", "tenant_id", "started_at"),
        Index("ix_investigations_tenant_status", "tenant_id", "status"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    request: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="queued")
    execution_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="assisted"
    )
    investigation_type: Mapped[str | None] = mapped_column(String(50))
    request_type: Mapped[str | None] = mapped_column(String(50))
    resource_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]", default=list
    )
    result: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    pending_question: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(String(1000))
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=_utcnow)


class InvestigationEvent(UUIDPkMixin, CreatedAtMixin, Base):
    __tablename__ = "investigation_events"
    __table_args__ = (
        UniqueConstraint("investigation_id", "seq"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["investigation_id", "tenant_id"],
            ["investigations.id", "investigations.tenant_id"],
            ondelete="CASCADE",
            name="fk_investigation_events_inv_tenant",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    investigation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    agent_name: Mapped[str | None] = mapped_column(String(50))
    event_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
