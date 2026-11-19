"""Remediation: policies, approval requests and execution jobs (phase 5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, CreatedAtMixin, UUIDPkMixin

RISK_LEVELS = ("low", "medium", "high", "critical")
APPROVAL_STATUSES = (
    "pending",
    "approved",
    "rejected",
    "changes_requested",
    "cancelled",
    "expired",
    "executed",
    "failed",
)
OPEN_APPROVAL_STATUSES = ("pending",)
JOB_STATUSES = ("running", "succeeded", "failed", "rejected")
VERIFICATION_STATUSES = ("pending", "passed", "failed", "skipped")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Policy(UUIDPkMixin, CreatedAtMixin, Base):
    """One remediation policy rule: what it matches and what it decides.

    ``rules`` is ``{"match": {...}, "effect": {...}}``; see ``app.remediation.policy``.
    Lower ``priority`` is evaluated first; later matching rules override earlier ones.
    """

    __tablename__ = "policies"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        UniqueConstraint("tenant_id", "name", name="uq_policies_tenant_name"),
        CheckConstraint("policy_type IN ('remediation')", name="policy_type"),
        Index("ix_policies_tenant_priority", "tenant_id", "priority"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    policy_type: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="remediation"
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    rules: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=_utcnow)


class Approval(UUIDPkMixin, CreatedAtMixin, Base):
    """A request to run one exact action on one resource for one investigation.

    ``action_hash`` binds the decision to the tool, its canonical arguments, the
    resource and the investigation: anything different needs a new approval.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["investigation_id", "tenant_id"],
            ["investigations.id", "investigations.tenant_id"],
            ondelete="CASCADE",
            name="fk_approvals_investigation_tenant",
        ),
        ForeignKeyConstraint(
            ["requested_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_approvals_requester_tenant",
        ),
        ForeignKeyConstraint(
            ["approved_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_approvals_approver_tenant",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_approvals_id_tenant"),
        CheckConstraint(f"status IN {APPROVAL_STATUSES!r}", name="status"),
        CheckConstraint(f"risk_level IN {RISK_LEVELS!r}", name="risk_level"),
        Index("ix_approvals_tenant_status", "tenant_id", "status", "created_at"),
        Index("ix_approvals_investigation", "investigation_id", "step_index"),
        # At most one open request per investigation step.
        Index(
            "uq_approvals_open_step",
            "investigation_id",
            "step_index",
            unique=True,
            postgresql_where="status = 'pending'",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    investigation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    step_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(60), nullable=False)  # tool name
    action_parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    resource_name: Mapped[str | None] = mapped_column(String(255))
    risk_level: Mapped[str] = mapped_column(String(10), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    expected_impact: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    evidence_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]", default=list
    )
    # Snapshot of the cited evidence, so approvers see it while the run is paused.
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]", default=list
    )
    policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    auto_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    decision_comment: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExecutionJob(UUIDPkMixin, Base):
    """One execution of a state-changing tool through the gateway."""

    __tablename__ = "execution_jobs"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["investigation_id", "tenant_id"],
            ["investigations.id", "investigations.tenant_id"],
            ondelete="CASCADE",
            name="fk_execution_jobs_investigation_tenant",
        ),
        ForeignKeyConstraint(
            ["approval_id", "tenant_id"],
            ["approvals.id", "approvals.tenant_id"],
            ondelete="CASCADE",
            name="fk_execution_jobs_approval_tenant",
        ),
        # An approval executes at most once.
        UniqueConstraint("approval_id", name="uq_execution_jobs_approval"),
        CheckConstraint(f"status IN {JOB_STATUSES!r}", name="status"),
        CheckConstraint(f"verification_status IN {VERIFICATION_STATUSES!r}", name="verification"),
        Index("ix_execution_jobs_tenant_started", "tenant_id", "started_at"),
        Index("ix_execution_jobs_resource", "resource_id", "started_at"),
        Index("ix_execution_jobs_investigation", "investigation_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    step_index: Mapped[int | None] = mapped_column(Integer)
    tool: Mapped[str] = mapped_column(String(60), nullable=False)
    # Snapshot, not a foreign key: the record outlives a deleted resource.
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    resource_name: Mapped[str | None] = mapped_column(String(255))
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    description: Mapped[str] = mapped_column(String(1000), nullable=False, server_default="")
    risk_level: Mapped[str] = mapped_column(String(10), nullable=False, server_default="high")
    actor: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="running")
    execution_result: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    before: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    after: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    verification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending"
    )
    verification: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
