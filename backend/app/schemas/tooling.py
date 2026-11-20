"""Schemas for tenant-registered tools (MCP server tools, custom HTTP tools)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ApiModel

ToolKind = Literal["read", "change"]
RiskLevel = Literal["low", "medium", "high", "critical"]
ToolPermission = Literal["tool.execute.readonly", "tool.execute.remediation"]
ToolStatus = Literal["new", "active", "schema_changed", "missing"]
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class ToolDefinitionOut(ApiModel):
    id: uuid.UUID
    integration_id: uuid.UUID
    integration_name: str | None = None
    source: str
    name: str
    remote_name: str | None
    title: str | None
    description: str
    remote_description: str | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    http_template: dict[str, Any] | None
    kind: str
    risk_level: str
    required_permission: str
    timeout_seconds: float
    verify_with: str | None
    enabled: bool
    status: str
    schema_hash: str
    approved_schema_hash: str | None
    approved_by: str | None
    last_synced_at: datetime | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime | None
    usable: bool = False  # enabled, active and approved: in the catalogue


class ToolDefinitionUpdate(BaseModel):
    description: str | None = Field(None, max_length=1000)
    kind: ToolKind | None = None
    risk_level: RiskLevel | None = None
    required_permission: ToolPermission | None = None
    timeout_seconds: float | None = Field(None, ge=1, le=600)
    verify_with: str | None = Field(None, max_length=60)
    clear_verify_with: bool = False
    enabled: bool | None = None
    approve_schema: bool = Field(
        False, description="Accept the tool's current schema and description (after review)"
    )


class ParamIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    type: Literal["string", "integer", "number", "boolean"] = "string"
    description: str = Field("", max_length=300)
    required: bool = True
    enum: list[str | int | float] | None = Field(None, max_length=50)
    pattern: str | None = Field(None, max_length=200)
    max_length: int | None = Field(None, ge=1, le=10000)
    minimum: float | None = None
    maximum: float | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _IDENT.match(v):
            raise ValueError("must be an identifier (letters, digits, underscore)")
        return v

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, v: str | None) -> str | None:
        if v:
            try:
                re.compile(v)
            except re.error as exc:
                raise ValueError(f"invalid regular expression: {exc}") from exc
        return v


class HttpTemplateIn(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str = Field("/", max_length=500)
    query: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    response_max_bytes: int = Field(200_000, ge=1000, le=2_000_000)


class HttpToolCreate(BaseModel):
    integration_id: uuid.UUID
    name: str = Field(min_length=1, max_length=50, description="Short name; prefixed with http_")
    description: str = Field(min_length=1, max_length=1000)
    parameters: list[ParamIn] = Field(default_factory=list, max_length=30)
    template: HttpTemplateIn
    kind: ToolKind = "read"
    risk_level: RiskLevel = "low"
    timeout_seconds: float = Field(30, ge=1, le=600)
    verify_with: str | None = Field(None, max_length=60)
    enabled: bool = True


class HttpToolUpdate(BaseModel):
    description: str | None = Field(None, min_length=1, max_length=1000)
    parameters: list[ParamIn] | None = Field(None, max_length=30)
    template: HttpTemplateIn | None = None


class ToolTestRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolSyncResult(BaseModel):
    server: dict[str, Any] = Field(default_factory=dict)
    total: int
    added: list[str]
    changed: list[str]
    missing: list[str]
    unchanged: int
