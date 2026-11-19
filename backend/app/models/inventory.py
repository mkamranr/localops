"""Infrastructure inventory: integrations, resources, relationships and discovery history."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
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

INTEGRATION_TYPES = ("linux_ssh", "windows_winrm", "docker", "http")
RESOURCE_TYPES = (
    "linux_server",
    "windows_server",
    "docker_host",
    "docker_container",
    "http_service",
    "application",
    "database",
    "network_device",
    "other",
)
HEALTH_STATUSES = ("healthy", "degraded", "unhealthy", "unknown")
RELATIONSHIP_TYPES = (
    "HOSTS",
    "DEPENDS_ON",
    "CONNECTS_TO",
    "RUNS_ON",
    "STORES_DATA_IN",
    "EXPOSES",
    "MONITORED_BY",
    "MANAGED_BY",
)
RUN_STATUSES = ("queued", "running", "completed", "partial", "failed")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Integration(UUIDPkMixin, CreatedAtMixin, Base):
    """A connection profile: type, settings, encrypted credentials and targets."""

    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name"),
        UniqueConstraint("id", "tenant_id", name="uq_integrations_id_tenant"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        CheckConstraint(f"integration_type IN {INTEGRATION_TYPES!r}", name="type"),
        CheckConstraint("schedule_minutes IS NULL OR schedule_minutes >= 5", name="schedule"),
        Index("ix_integrations_due", "next_run_at", postgresql_where="enabled"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    integration_type: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    encrypted_credentials: Mapped[str | None] = mapped_column(Text)
    targets: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]", default=list
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    schedule_minutes: Mapped[int | None] = mapped_column(Integer)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(20))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=_utcnow)


class Resource(UUIDPkMixin, CreatedAtMixin, Base):
    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_resources_id_tenant"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["integration_id", "tenant_id"],
            ["integrations.id", "integrations.tenant_id"],
            name="fk_resources_integration_tenant",
        ),
        CheckConstraint(f"resource_type IN {RESOURCE_TYPES!r}", name="type"),
        CheckConstraint(f"health_status IN {HEALTH_STATUSES!r}", name="health"),
        CheckConstraint("source IN ('discovered', 'manual')", name="source"),
        # Rediscovery (by any integration) updates the same row.
        Index(
            "uq_resources_external_id",
            "tenant_id",
            "external_id",
            unique=True,
            postgresql_where="external_id IS NOT NULL",
        ),
        Index("ix_resources_tenant_type", "tenant_id", "resource_type"),
        Index("ix_resources_tenant_name", "tenant_id", "name"),
        Index("ix_resources_integration", "integration_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(30), nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(255))
    ip_addresses: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]", default=list
    )
    os: Mapped[str | None] = mapped_column(String(200))
    environment: Mapped[str | None] = mapped_column(String(50))
    group_name: Mapped[str | None] = mapped_column(String(100))
    owner: Mapped[str | None] = mapped_column(String(100))
    tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]", default=list
    )
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # Collected facts (services, ports, disks, container details...). Never credentials.
    facts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    health_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="unknown", default="unknown"
    )
    health_reasons: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]", default=list
    )
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="manual", default="manual"
    )
    integration_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    external_id: Mapped[str | None] = mapped_column(String(300))
    discovery_target: Mapped[str | None] = mapped_column(String(300))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stale: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=_utcnow)


class ResourceRelationship(UUIDPkMixin, CreatedAtMixin, Base):
    __tablename__ = "resource_relationships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_id", "target_id", "relationship_type"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["source_id", "tenant_id"],
            ["resources.id", "resources.tenant_id"],
            ondelete="CASCADE",
            name="fk_relationships_source_tenant",
        ),
        ForeignKeyConstraint(
            ["target_id", "tenant_id"],
            ["resources.id", "resources.tenant_id"],
            ondelete="CASCADE",
            name="fk_relationships_target_tenant",
        ),
        CheckConstraint(f"relationship_type IN {RELATIONSHIP_TYPES!r}", name="type"),
        CheckConstraint("source IN ('discovered', 'manual')", name="source"),
        CheckConstraint("source_id <> target_id", name="no_self"),
        Index("ix_relationships_target", "target_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="manual", default="manual"
    )
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="1.0", default=1.0
    )
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DiscoveryRun(UUIDPkMixin, Base):
    __tablename__ = "discovery_runs"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["integration_id", "tenant_id"],
            ["integrations.id", "integrations.tenant_id"],
            ondelete="CASCADE",
            name="fk_discovery_runs_integration_tenant",
        ),
        CheckConstraint(f"status IN {RUN_STATUSES!r}", name="status"),
        CheckConstraint("trigger IN ('manual', 'scheduled')", name="trigger"),
        Index("ix_discovery_runs_integration", "integration_id", "started_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    integration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="queued")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    target_results: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]", default=list
    )
    error: Mapped[str | None] = mapped_column(String(1000))


class ResourceEvent(UUIDPkMixin, CreatedAtMixin, Base):
    __tablename__ = "resource_events"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["resource_id", "tenant_id"],
            ["resources.id", "resources.tenant_id"],
            ondelete="CASCADE",
            name="fk_resource_events_resource_tenant",
        ),
        Index("ix_resource_events_resource", "resource_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    actor: Mapped[str | None] = mapped_column(String(100))


class ResourceMetricSample(UUIDPkMixin, Base):
    __tablename__ = "resource_metric_samples"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["resource_id", "tenant_id"],
            ["resources.id", "resources.tenant_id"],
            ondelete="CASCADE",
            name="fk_metric_samples_resource_tenant",
        ),
        Index("ix_metric_samples_resource", "resource_id", "taken_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    taken_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    cpu_load: Mapped[float | None] = mapped_column(Float)
    cpu_count: Mapped[int | None] = mapped_column(Integer)
    memory_percent: Mapped[float | None] = mapped_column(Float)
    disk_percent: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[float | None] = mapped_column(Float)


class SshHostKey(UUIDPkMixin, Base):
    """Trust-on-first-use pin of a target's SSH host key."""

    __tablename__ = "ssh_host_keys"
    __table_args__ = (
        UniqueConstraint("tenant_id", "host", "port"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    key_type: Mapped[str] = mapped_column(String(50), nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(100), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
