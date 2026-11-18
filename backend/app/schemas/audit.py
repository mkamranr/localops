"""Audit log and dashboard schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator

from app.schemas.common import ApiModel


class AuditLogOut(ApiModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    username: str | None
    action: str
    outcome: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    details: dict[str, Any]
    # asyncpg returns INET as ipaddress objects
    ip_address: Annotated[str | None, BeforeValidator(lambda v: None if v is None else str(v))]
    request_id: str | None
    created_at: datetime


class ServiceHealth(BaseModel):
    name: str
    status: str  # "ok" | "error"
    latency_ms: float | None = None
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: str  # "ready" | "degraded"
    services: list[ServiceHealth]


class CountSummary(BaseModel):
    total: int = 0
    online: int = 0
    offline: int = 0


class IncidentSummary(BaseModel):
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    open_total: int = 0


class UserSummary(BaseModel):
    total: int
    active: int
    disabled: int


class ActivitySummary(BaseModel):
    events_24h: int
    logins_24h: int
    failed_logins_24h: int


class DashboardSummary(BaseModel):
    tenant: str
    users: UserSummary
    activity: ActivitySummary
    services: list[ServiceHealth]
    # Placeholders until Phases 2-5 introduce the underlying data.
    resources: CountSummary
    incidents: IncidentSummary
    active_investigations: int = 0
    pending_approvals: int = 0
    knowledge_documents: int = 0
