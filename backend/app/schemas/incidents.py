"""Incident, timeline and diagnostic-tool schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field, field_validator

from app.models.incidents import incident_key
from app.schemas.common import ApiModel

Severity = Literal["critical", "high", "medium", "low", "informational"]
Priority = Literal["P1", "P2", "P3", "P4"]
IncidentStatus = Literal[
    "new",
    "investigating",
    "awaiting_approval",
    "remediating",
    "monitoring",
    "resolved",
    "closed",
    "failed",
]
IncidentSource = Literal["manual", "investigation", "discovery"]
ExecutionMode = Literal["manual", "assisted", "autonomous"]


def _text(v: str | None) -> str | None:
    return v.strip() if isinstance(v, str) else v


class IncidentCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field("", max_length=8000)
    severity: Severity = "medium"
    priority: Priority | None = None  # derived from severity when omitted
    resource_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    assigned_to: uuid.UUID | None = None
    investigation_id: uuid.UUID | None = None  # create from a completed investigation

    _strip = field_validator("title", "description")(_text)


class IncidentUpdate(BaseModel):
    title: str | None = Field(None, min_length=3, max_length=200)
    description: str | None = Field(None, max_length=8000)
    severity: Severity | None = None
    priority: Priority | None = None
    status: IncidentStatus | None = None
    assigned_to: uuid.UUID | None = None
    resolution: str | None = Field(None, max_length=8000)
    resource_ids: list[uuid.UUID] | None = Field(None, max_length=20)

    _strip = field_validator("title", "description", "resolution")(_text)


class IncidentClose(BaseModel):
    resolution: str = Field(min_length=3, max_length=8000)
    status: Literal["resolved", "closed"] = "closed"


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class IncidentInvestigate(BaseModel):
    request: str | None = Field(None, max_length=4000)
    execution_mode: ExecutionMode = "assisted"


class IncidentResourceOut(ApiModel):
    id: uuid.UUID
    name: str
    resource_type: str
    health_status: str
    environment: str | None = None


class IncidentOut(ApiModel):
    id: uuid.UUID
    number: int
    title: str
    description: str
    severity: str
    priority: str
    status: str
    source: str
    assigned_to: uuid.UUID | None
    assigned_username: str | None = None
    created_by: str | None
    root_cause: dict[str, Any]
    investigation_summary: str | None
    resolution: str | None
    created_at: datetime
    updated_at: datetime | None
    resolved_at: datetime | None
    closed_at: datetime | None
    resources: list[IncidentResourceOut] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def key(self) -> str:
        return incident_key(self.number)


class LinkedInvestigation(ApiModel):
    id: uuid.UUID
    title: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    conclusion: str | None = None


class IncidentDetail(IncidentOut):
    investigations: list[LinkedInvestigation] = Field(default_factory=list)
    allowed_transitions: list[str] = Field(default_factory=list)


class IncidentEventOut(ApiModel):
    id: uuid.UUID
    event_type: str
    summary: str
    details: dict[str, Any]
    actor: str | None
    created_at: datetime


class SimilarIncident(BaseModel):
    kind: str
    id: uuid.UUID
    number: str | None = None
    title: str
    status: str
    severity: str | None = None
    root_cause: str | None = None
    resolution: str | None = None
    conclusion: str | None = None
    created_at: str
    score: float


class IncidentStats(BaseModel):
    open: int
    by_status: dict[str, int]
    by_severity_open: dict[str, int]
    resolved_last_7d: int
    mean_time_to_resolve_minutes: float | None


class ToolExecutionOut(ApiModel):
    id: uuid.UUID
    investigation_id: uuid.UUID | None
    resource_id: uuid.UUID | None
    resource_name: str | None
    agent_type: str | None
    actor: str | None
    evidence_id: str | None
    tool: str
    arguments: dict[str, Any]
    status: str
    duration_ms: int | None
    output: str | None
    summary: str | None
    error: str | None
    created_at: datetime


class ToolOut(BaseModel):
    name: str
    description: str
    category: str
    risk_level: str
    requires_approval: bool
    remote: bool
    timeout_seconds: int
    resource_types: list[str]
    input_schema: dict[str, Any]
    kind: str = "read"
    permission: str = "tool.execute.readonly"
    verify_with: list[str] = Field(default_factory=list)


class ToolRunRequest(BaseModel):
    resource_id: uuid.UUID
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolRunOut(BaseModel):
    tool: str
    status: str
    summary: str
    output: str
    error: str | None
    duration_ms: int
    execution_id: str | None
    resource_name: str | None
