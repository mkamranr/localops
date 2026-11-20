"""Tenant-registered tools: MCP server tools and custom HTTP request templates (phase 6).

Built-in tools live in code (``app.tools.registry``); these rows extend the catalogue
per tenant. A row is usable only when ``enabled`` and ``status == 'active'``.
"""

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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, CreatedAtMixin, UUIDPkMixin

TOOL_SOURCES = ("mcp", "http")
TOOL_KINDS = ("read", "change")
TOOL_STATUSES = ("new", "active", "schema_changed", "missing")
TOOL_PERMISSIONS = ("tool.execute.readonly", "tool.execute.remediation")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ToolDefinition(UUIDPkMixin, CreatedAtMixin, Base):
    __tablename__ = "tool_definitions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_tool_definitions_tenant_name"),
        UniqueConstraint("id", "tenant_id", name="uq_tool_definitions_id_tenant"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["integration_id", "tenant_id"],
            ["integrations.id", "integrations.tenant_id"],
            ondelete="CASCADE",
            name="fk_tool_definitions_integration_tenant",
        ),
        CheckConstraint(f"source IN {TOOL_SOURCES!r}", name="source"),
        CheckConstraint(f"kind IN {TOOL_KINDS!r}", name="kind"),
        CheckConstraint(f"status IN {TOOL_STATUSES!r}", name="status"),
        CheckConstraint("risk_level IN ('low', 'medium', 'high', 'critical')", name="risk"),
        CheckConstraint(f"required_permission IN {TOOL_PERMISSIONS!r}", name="permission"),
        CheckConstraint("name ~ '^[a-z][a-z0-9_]{0,59}$'", name="name"),
        Index("ix_tool_definitions_integration", "integration_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    integration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    remote_name: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    remote_description: Mapped[str | None] = mapped_column(Text)
    input_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    http_template: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    kind: Mapped[str] = mapped_column(String(10), nullable=False, server_default="read")
    risk_level: Mapped[str] = mapped_column(String(10), nullable=False, server_default="low")
    required_permission: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="tool.execute.readonly"
    )
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, server_default="30")
    verify_with: Mapped[str | None] = mapped_column(String(60))
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="new")
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_schema_hash: Mapped[str | None] = mapped_column(String(64))
    approved_by: Mapped[str | None] = mapped_column(String(100))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=_utcnow)
