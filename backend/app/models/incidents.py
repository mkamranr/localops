"""Incidents, their timeline, and the record of diagnostic tool executions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
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
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, CreatedAtMixin, UUIDPkMixin

SEVERITIES = ("critical", "high", "medium", "low", "informational")
PRIORITIES = ("P1", "P2", "P3", "P4")
INCIDENT_STATUSES = (
    "new",
    "investigating",
    "awaiting_approval",
    "remediating",
    "monitoring",
    "resolved",
    "closed",
    "failed",
)
OPEN_STATUSES = ("new", "investigating", "awaiting_approval", "remediating", "monitoring")
INCIDENT_SOURCES = ("manual", "investigation", "discovery")
TOOL_STATUSES = ("success", "failed", "rejected")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Incident(UUIDPkMixin, CreatedAtMixin, Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_incidents_id_tenant"),
        UniqueConstraint("tenant_id", "number", name="uq_incidents_tenant_number"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["assigned_to", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_incidents_assignee_tenant",
        ),
        CheckConstraint(f"severity IN {SEVERITIES!r}", name="severity"),
        CheckConstraint(f"priority IN {PRIORITIES!r}", name="priority"),
        CheckConstraint(f"status IN {INCIDENT_STATUSES!r}", name="status"),
        CheckConstraint(f"source IN {INCIDENT_SOURCES!r}", name="source"),
        Index("ix_incidents_tenant_status", "tenant_id", "status"),
        Index("ix_incidents_tenant_created", "tenant_id", "created_at"),
        # One open incident per de-duplication key (automatic incidents).
        Index(
            "uq_incidents_open_dedup",
            "tenant_id",
            "dedup_key",
            unique=True,
            postgresql_where=(
                "dedup_key IS NOT NULL AND status IN "
                "('new','investigating','awaiting_approval','remediating','monitoring')"
            ),
        ),
        Index("ix_incidents_search", "search_vector", postgresql_using="gin"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    severity: Mapped[str] = mapped_column(String(20), nullable=False, server_default="medium")
    priority: Mapped[str] = mapped_column(String(2), nullable=False, server_default="P3")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="new")
    source: Mapped[str] = mapped_column(String(20), nullable=False, server_default="manual")
    dedup_key: Mapped[str | None] = mapped_column(String(200))
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[str | None] = mapped_column(String(100))
    root_cause: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    investigation_summary: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, '')"
            " || ' ' || coalesce(investigation_summary, '') || ' ' ||"
            " coalesce(root_cause ->> 'statement', '') || ' ' || coalesce(resolution, ''))",
            persisted=True,
        ),
    )


class IncidentResource(Base):
    __tablename__ = "incident_resources"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["incident_id", "tenant_id"],
            ["incidents.id", "incidents.tenant_id"],
            ondelete="CASCADE",
            name="fk_incident_resources_incident_tenant",
        ),
        ForeignKeyConstraint(
            ["resource_id", "tenant_id"],
            ["resources.id", "resources.tenant_id"],
            ondelete="CASCADE",
            name="fk_incident_resources_resource_tenant",
        ),
        Index("ix_incident_resources_resource", "resource_id"),
    )

    incident_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class IncidentEvent(UUIDPkMixin, CreatedAtMixin, Base):
    """Incident timeline entry (append-only)."""

    __tablename__ = "incident_events"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["incident_id", "tenant_id"],
            ["incidents.id", "incidents.tenant_id"],
            ondelete="CASCADE",
            name="fk_incident_events_incident_tenant",
        ),
        Index("ix_incident_events_incident", "incident_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    incident_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    summary: Mapped[str] = mapped_column(String(1000), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    actor: Mapped[str | None] = mapped_column(String(100))


class ToolExecution(UUIDPkMixin, CreatedAtMixin, Base):
    """One run of a read-only diagnostic tool, by an agent or a user."""

    __tablename__ = "tool_executions"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["investigation_id", "tenant_id"],
            ["investigations.id", "investigations.tenant_id"],
            ondelete="CASCADE",
            name="fk_tool_executions_investigation_tenant",
        ),
        CheckConstraint(f"status IN {TOOL_STATUSES!r}", name="status"),
        Index("ix_tool_executions_investigation", "investigation_id", "created_at"),
        Index("ix_tool_executions_resource", "resource_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Snapshot, not a foreign key: the record outlives a deleted resource.
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    resource_name: Mapped[str | None] = mapped_column(String(255))
    agent_type: Mapped[str | None] = mapped_column(String(50))
    actor: Mapped[str | None] = mapped_column(String(100))
    evidence_id: Mapped[str | None] = mapped_column(String(10))
    tool: Mapped[str] = mapped_column(String(60), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    output: Mapped[str | None] = mapped_column(Text)  # redacted, capped excerpt
    summary: Mapped[str | None] = mapped_column(String(500))
    error: Mapped[str | None] = mapped_column(String(1000))


def incident_key(number: int) -> str:
    """Display form of an incident number, e.g. ``INC-0042``."""
    return f"INC-{number:04d}"
