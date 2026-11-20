"""Read-only diagnostic tools: specification, context and results.

A tool is a fixed, parameterised read-only operation. Its arguments are a Pydantic
model with strict validators; executors build commands from constant templates and
never interpolate unvalidated text. The model only ever sees the tool's name,
description and JSON schema.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from app.tools.context import ToolContext


class ToolError(Exception):
    """The tool could not run or failed; ``code`` is machine-readable."""

    def __init__(self, message: str, *, code: str = "tool_failed") -> None:
        super().__init__(message)
        self.code = code


class StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class JsonArgs(StrictArgs):
    """Arguments of a tenant-registered tool (MCP / HTTP template).

    Pydantic only accepts a JSON object here; the tool's own JSON Schema (checked by
    ``ToolSpec.validator``) decides what is valid.
    """

    model_config = ConfigDict(extra="allow", str_strip_whitespace=False)


class ArgumentsError(ValueError):
    """Tool arguments failed validation; the message is safe to show a model."""


class ResourceArgs(StrictArgs):
    resource_id: str = Field(
        description="Id of the resource (from the list of resources in scope)",
        min_length=1,
        max_length=255,
    )


@dataclass(slots=True)
class ToolOutput:
    """``text`` is what the model reads (redacted and capped by the executor)."""

    text: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)


Runner = Callable[["ToolContext", Any], Awaitable[ToolOutput]]
# Human-readable, sanitised statement of exactly what a change tool will do.
Describer = Callable[[Any, "ResourceInfo"], str]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    args: type[StrictArgs]
    run: Runner
    # Resource types the tool applies to; empty means "no resource" (tenant-wide).
    resource_types: tuple[str, ...] = ()
    remote: bool = True  # contacts a managed host (needs tool.execute.readonly)
    timeout_seconds: float = 45.0
    category: str = "general"
    # Phase 5: ``change`` tools alter state and only run through the execution gateway.
    kind: str = "read"  # read | change
    default_risk: str = "low"  # low | medium | high | critical
    describe_action: Describer | None = None
    # Read tools that show whether a change worked (verification).
    verify_with: tuple[str, ...] = ()
    # Phase 6: tenant-registered tools (MCP servers, HTTP templates).
    source: str = "builtin"  # builtin | mcp | http
    input_schema: dict[str, Any] | None = None  # JSON Schema replacing the args model's
    validator: Callable[[dict[str, Any]], None] | None = None  # raises ArgumentsError
    required_permission: str | None = None
    integration_id: uuid.UUID | None = None
    definition_id: uuid.UUID | None = None

    @property
    def is_change(self) -> bool:
        return self.kind == "change"

    @property
    def permission(self) -> str:
        if self.required_permission:
            return self.required_permission
        return "tool.execute.remediation" if self.is_change else "tool.execute.readonly"

    def validate_args(self, raw: dict[str, Any] | None) -> StrictArgs:
        """Validate arguments; raises ``ArgumentsError`` with a model-readable message."""
        try:
            args = self.args.model_validate(dict(raw or {}))
        except ValidationError as exc:
            parts = []
            for err in exc.errors()[:6]:
                loc = ".".join(str(p) for p in err["loc"]) or "arguments"
                parts.append(f"{loc}: {err['msg']}")
            raise ArgumentsError("; ".join(parts)) from exc
        if self.validator is not None:
            self.validator(args.model_dump(mode="json"))
        return args

    def declares(self, field_name: str) -> bool:
        """Whether the arguments model itself defines ``field_name`` (not an extra)."""
        return field_name in self.args.model_fields

    @property
    def needs_resource(self) -> bool:
        return issubclass(self.args, ResourceArgs)

    def json_schema(self) -> dict[str, Any]:
        if self.input_schema is not None:
            return dict(self.input_schema)
        schema = self.args.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return schema

    def openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.json_schema(),
            },
        }

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "resource_types": list(self.resource_types),
            "remote": self.remote,
            "kind": self.kind,
            "source": self.source,
            "risk_level": self.default_risk if self.is_change else "read",
            "requires_approval": self.is_change,
            "permission": self.permission,
            "verify_with": list(self.verify_with),
            "timeout_seconds": self.timeout_seconds,
            "input_schema": self.json_schema(),
        }


@dataclass(slots=True)
class ResourceInfo:
    """What a tool may know about its target resource (no credentials)."""

    id: uuid.UUID
    name: str
    resource_type: str
    hostname: str | None
    ip_addresses: list[str]
    integration_id: uuid.UUID | None
    discovery_target: str | None
    facts: dict[str, Any]
    health_status: str
    environment: str | None = None
    group_name: str | None = None
    tags: list[str] = field(default_factory=list)
