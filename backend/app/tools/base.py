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

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from app.tools.context import ToolContext


class ToolError(Exception):
    """The tool could not run or failed; ``code`` is machine-readable."""

    def __init__(self, message: str, *, code: str = "tool_failed") -> None:
        super().__init__(message)
        self.code = code


class StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


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

    @property
    def needs_resource(self) -> bool:
        return issubclass(self.args, ResourceArgs)

    def json_schema(self) -> dict[str, Any]:
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
            "risk_level": "read",
            "requires_approval": False,
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
