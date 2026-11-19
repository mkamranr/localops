"""API models for policies, approvals and remediation executions (phase 5)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.remediation.policy import PolicyRules

ApprovalStatus = Literal[
    "pending",
    "approved",
    "rejected",
    "changes_requested",
    "cancelled",
    "expired",
    "executed",
    "failed",
]
RiskLevel = Literal["low", "medium", "high", "critical"]


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------ policies


class PolicyIn(BaseModel):
    name: str = Field(min_length=3, max_length=100)
    description: str = Field("", max_length=1000)
    priority: int = Field(100, ge=0, le=10_000)
    rules: PolicyRules = Field(default_factory=PolicyRules)
    enabled: bool = True

    @field_validator("name", "description")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class PolicyUpdate(BaseModel):
    name: str | None = Field(None, min_length=3, max_length=100)
    description: str | None = Field(None, max_length=1000)
    priority: int | None = Field(None, ge=0, le=10_000)
    rules: PolicyRules | None = None
    enabled: bool | None = None


class PolicyOut(ApiModel):
    id: uuid.UUID
    name: str
    description: str
    policy_type: str
    priority: int
    rules: dict[str, Any]
    enabled: bool
    builtin: bool
    created_at: datetime
    updated_at: datetime | None


class PolicyPreviewIn(BaseModel):
    tool: str = Field(max_length=60)
    resource_id: uuid.UUID
    execution_mode: Literal["manual", "assisted", "autonomous"] = "assisted"


# ------------------------------------------------------------------ approvals


class ApprovalOut(ApiModel):
    id: uuid.UUID
    investigation_id: uuid.UUID
    incident_id: uuid.UUID | None
    step_index: int
    requested_by: uuid.UUID
    requested_by_username: str | None = None
    approved_by: uuid.UUID | None
    approved_by_username: str | None = None
    action: str
    action_parameters: dict[str, Any]
    description: str
    resource_id: uuid.UUID | None
    resource_name: str | None
    risk_level: str
    rationale: str
    expected_impact: str
    evidence_ids: list[str]
    policy: dict[str, Any]
    auto_approved: bool
    status: str
    decision_comment: str | None
    expires_at: datetime
    decided_at: datetime | None
    created_at: datetime


class ApprovalDetail(ApprovalOut):
    investigation_title: str | None = None
    incident_key: str | None = None
    can_decide: bool = False
    cannot_decide_reason: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    execution: ExecutionOut | None = None


class ApprovalDecision(BaseModel):
    comment: str | None = Field(None, max_length=2000)

    @field_validator("comment")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None


class ChangesRequest(BaseModel):
    comment: str = Field(min_length=3, max_length=2000)


class ApprovalSummary(BaseModel):
    pending: int
    pending_for_me: int
    by_risk: dict[str, int]
    expiring_soon: int


# ------------------------------------------------------------------ executions


class ExecutionOut(ApiModel):
    id: uuid.UUID
    investigation_id: uuid.UUID | None
    approval_id: uuid.UUID | None
    step_index: int | None
    tool: str
    resource_id: uuid.UUID | None
    resource_name: str | None
    parameters: dict[str, Any]
    description: str
    risk_level: str
    actor: str | None
    status: str
    execution_result: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]
    verification_status: str
    verification: dict[str, Any]
    started_at: datetime
    completed_at: datetime | None
    duration_ms: int | None


ApprovalDetail.model_rebuild()
