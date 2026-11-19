"""Schemas for model endpoints, agents and investigations."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.schemas.common import ApiModel

Provider = Literal["vllm", "ollama", "llamacpp", "openai_compatible"]
ExecutionMode = Literal["manual", "assisted", "autonomous"]


def _check_url(v: str) -> str:
    v = v.strip().rstrip("/")
    if not v.startswith(("http://", "https://")):
        raise ValueError("must start with http:// or https://")
    if any(c.isspace() for c in v):
        raise ValueError("must not contain whitespace")
    return v


# ----------------------------------------------------------------- models


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: int = Field(120, ge=5, le=900)
    max_retries: int = Field(2, ge=0, le=5)
    structured_output: Literal["json_schema", "json_object", "none"] = "json_object"
    context_window: int | None = Field(None, ge=512, le=2_000_000)
    # "auto": allow for reasoning output; "enabled": the model always thinks (bigger
    # budget); "disabled": ask the server to switch reasoning off where supported.
    thinking: Literal["auto", "enabled", "disabled"] = "auto"
    # How diagnostic agents call tools: OpenAI "tools" (native), a JSON action in the
    # prompt (json), or native with automatic fallback to json (auto).
    tool_calling: Literal["auto", "native", "json"] = "auto"


class ModelEndpointCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: Provider
    base_url: str = Field(max_length=500)
    model_name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=1000)
    configuration: ModelSettings = Field(default_factory=lambda: ModelSettings())
    api_key: SecretStr | None = None
    enabled: bool = True
    is_default: bool = False

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _check_url(v)


class ModelEndpointUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    provider: Provider | None = None
    base_url: str | None = Field(None, max_length=500)
    model_name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=1000)
    configuration: ModelSettings | None = None
    api_key: SecretStr | None = Field(None, description="New API key; omit to keep the current")
    clear_api_key: bool = False
    enabled: bool | None = None
    is_default: bool | None = None

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return _check_url(v) if v is not None else v


class ModelEndpointOut(ApiModel):
    id: uuid.UUID
    name: str
    provider: str
    base_url: str
    model_name: str
    description: str
    configuration: dict[str, Any]
    has_api_key: bool = False
    enabled: bool
    is_default: bool
    last_checked_at: datetime | None
    last_status: str | None
    last_latency_ms: float | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime | None


class ModelTestResult(BaseModel):
    ok: bool
    latency_ms: float | None
    models: list[str]
    model_available: bool | None
    reply: str | None = None
    error: str | None = None


# ----------------------------------------------------------------- agents


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    temperature: float | None = Field(None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(None, ge=64, le=32_768)
    timeout_seconds: int | None = Field(None, ge=5, le=900)
    max_retries: int | None = Field(None, ge=0, le=5)


class AgentStats(BaseModel):
    executions_24h: int = 0
    failures_24h: int = 0
    avg_duration_ms: float | None = None
    tokens_24h: int = 0
    last_run_at: datetime | None = None


class AgentOut(BaseModel):
    id: uuid.UUID
    agent_type: str
    name: str
    description: str
    enabled: bool
    model_id: uuid.UUID | None
    model_name: str | None
    configuration: dict[str, Any]
    capabilities: list[str]
    available_in_phase: int
    implemented: bool
    specialist: bool
    stats: AgentStats
    updated_at: datetime | None


class AgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=1000)
    model_id: uuid.UUID | None = Field(None, description="null = use the tenant default model")
    configuration: AgentSettings | None = None
    enabled: bool | None = None


class AgentExecutionOut(ApiModel):
    id: uuid.UUID
    investigation_id: uuid.UUID | None
    agent_type: str
    node: str
    status: str
    model_name: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    llm_calls: int
    error: str | None
    started_at: datetime
    completed_at: datetime | None
    duration_ms: int | None


# ---------------------------------------------------------- investigations


class InvestigationCreate(BaseModel):
    request: str = Field(min_length=3, max_length=4000)
    title: str | None = Field(None, max_length=200)
    execution_mode: ExecutionMode = "assisted"
    # Resources to investigate regardless of what the request names (spec §10.5).
    resource_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    incident_id: uuid.UUID | None = None
    investigation_type: str | None = Field(None, max_length=50)

    @field_validator("request")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("request is too short")
        return v


class InvestigationResume(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)


class RemediationStepAction(BaseModel):
    action: Literal["request", "skip"]


class ReportBrief(ApiModel):
    """The headline of a completed report, for lists."""

    conclusion: str | None = None
    root_cause: str | None = None
    root_cause_status: str | None = None
    confidence: float | None = None

    @classmethod
    def from_result(cls, result: dict[str, Any] | None) -> ReportBrief | None:
        if not result or not result.get("conclusion"):
            return None
        raw = result.get("root_cause")
        cause: dict[str, Any] = raw if isinstance(raw, dict) else {}
        conf = result.get("confidence")
        return cls(
            conclusion=str(result["conclusion"]),
            root_cause=cause.get("statement"),
            root_cause_status=cause.get("status"),
            confidence=float(conf) if isinstance(conf, int | float) else None,
        )


class InvestigationOut(ApiModel):
    id: uuid.UUID
    title: str
    request: str
    status: str
    execution_mode: str
    request_type: str | None
    workflow_id: str
    user_id: uuid.UUID
    incident_id: uuid.UUID | None = None
    investigation_type: str | None = None
    pending_question: str | None
    error: str | None
    cancel_requested: bool
    attempts: int
    started_at: datetime
    completed_at: datetime | None
    updated_at: datetime | None
    report: ReportBrief | None = None


class InvestigationDetail(InvestigationOut):
    result: dict[str, Any]
    resource_ids: list[str] = Field(default_factory=list)
    username: str | None = None


class InvestigationEventOut(ApiModel):
    seq: int
    event_type: str
    agent_name: str | None
    event_data: dict[str, Any]
    created_at: datetime
