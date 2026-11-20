"""Turn tenant tool definitions (MCP tools, HTTP templates) into ``ToolSpec`` objects.

Arguments are validated against the definition's JSON Schema (Draft 2020-12). The
runner re-reads the definition at execution time, so disabling a tool, an unapproved
schema change or a disabled integration takes effect immediately — even for a
catalogue that was cached a moment earlier.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.core.logging import get_logger
from app.discovery.engine import load_credentials
from app.integrations import http_api
from app.integrations.mcp import McpClient, McpError, config_from_integration
from app.models import Integration, ToolDefinition
from app.tools.base import ArgumentsError, JsonArgs, ToolError, ToolOutput, ToolSpec
from app.tools.context import ToolContext

log = get_logger("localops.tools.dynamic")

MAX_OUTPUT_CHARS = 200_000
DESCRIPTION_CHARS = 1000
_NAME_PART = re.compile(r"[^a-z0-9]+")


def schema_hash(input_schema: dict[str, Any], description: str | None) -> str:
    """Fingerprint of what an admin approved: the schema *and* the remote description."""
    payload = json.dumps(
        {"schema": input_schema, "description": description or ""},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def tool_name(prefix: str, scope: str, remote: str) -> str:
    """``mcp_<scope>_<tool>`` style, lower-case, ``[a-z0-9_]``, at most 60 characters."""

    def part(text: str) -> str:
        return _NAME_PART.sub("_", text.lower()).strip("_")

    base = "_".join(p for p in (prefix, part(scope)[:20], part(remote)) if p)
    if len(base) > 60:
        digest = hashlib.sha256(base.encode()).hexdigest()[:6]
        base = f"{base[:53].rstrip('_')}_{digest}"
    if not base[0].isalpha():
        base = f"t_{base}"[:60]
    return base


def normalise_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Check a tool's argument schema; tool arguments are always a JSON object."""
    schema = dict(schema or {})
    schema.setdefault("type", "object")
    if schema.get("type") != "object":
        raise ArgumentsError("The argument schema must describe a JSON object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ArgumentsError(f"Invalid JSON Schema: {exc.message}") from exc
    schema.setdefault("properties", {})
    return schema


def make_validator(schema: dict[str, Any]) -> Any:
    validator = Draft202012Validator(schema)

    def check(values: dict[str, Any]) -> None:
        errors = sorted(validator.iter_errors(values), key=lambda e: list(e.path))
        if errors:
            parts = []
            for err in errors[:6]:
                loc = ".".join(str(p) for p in err.path) or "arguments"
                parts.append(f"{loc}: {err.message[:200]}")
            raise ArgumentsError("; ".join(parts))

    return check


async def _load(
    ctx: ToolContext, definition_id: uuid.UUID
) -> tuple[ToolDefinition, Integration, dict[str, Any]]:
    async with ctx.db() as session:
        row = await session.get(ToolDefinition, definition_id)
        if row is None or not row.enabled or row.status != "active":
            raise ToolError("This tool is disabled or awaiting review", code="tool_disabled")
        if row.approved_schema_hash != row.schema_hash:
            raise ToolError("This tool changed since it was approved", code="tool_changed")
        integration = await session.get(Integration, row.integration_id)
        if integration is None or not integration.enabled:
            raise ToolError("The tool's integration is missing or disabled", code="no_integration")
        credentials = load_credentials(ctx.settings, integration)
        session.expunge(row)
        session.expunge(integration)
    return row, integration, credentials


def _runner(definition_id: uuid.UUID) -> Any:
    async def run(ctx: ToolContext, args: JsonArgs) -> ToolOutput:
        row, integration, credentials = await _load(ctx, definition_id)
        values = args.model_dump(mode="json")
        config = dict(integration.configuration or {})
        if row.source == "mcp":
            try:
                async with McpClient(
                    config_from_integration(config, credentials, row.timeout_seconds)
                ) as client:
                    result = await client.call_tool(row.remote_name or row.name, values)
            except McpError as exc:
                raise ToolError(f"MCP server: {exc}", code=exc.code) from exc
            text = result.text[:MAX_OUTPUT_CHARS] or "(empty result)"
            if result.is_error:
                raise ToolError(f"The MCP tool reported an error: {text[:500]}", code="tool_error")
            first = text.strip().splitlines()[0][:160] if text.strip() else "empty result"
            return ToolOutput(
                text,
                f"{integration.name} · {row.remote_name}: {first}",
                {"structured": result.structured} if result.structured is not None else {},
            )
        if row.source == "http":
            text, summary, data = await http_api.execute(
                config,
                credentials,
                row.http_template or {},
                values,
                timeout_seconds=row.timeout_seconds,
            )
            return ToolOutput(text[:MAX_OUTPUT_CHARS], f"{integration.name} · {summary}", data)
        raise ToolError(f"Unsupported tool source {row.source!r}", code="unsupported")

    return run


def _describe_change(row: ToolDefinition) -> Any:
    def describe(args: Any, resource: Any) -> str:
        values = json.dumps(args.model_dump(mode="json"), sort_keys=True)[:600]
        where = getattr(resource, "name", "?")
        kind = "MCP tool" if row.source == "mcp" else "HTTP request"
        return f"Run {kind} '{row.remote_name or row.name}' on {where} with arguments {values}"

    return describe


def spec_from_definition(row: ToolDefinition) -> ToolSpec | None:
    """A usable spec, or None when the stored schema is no longer valid."""
    try:
        schema = normalise_schema(row.input_schema)
    except ArgumentsError:
        log.warning("tool_schema_invalid", tool=row.name)
        return None
    return ToolSpec(
        name=row.name,
        description=(row.description or row.remote_description or row.name)[:DESCRIPTION_CHARS],
        args=JsonArgs,
        run=_runner(row.id),
        resource_types=(),
        remote=True,
        timeout_seconds=float(row.timeout_seconds),
        category="mcp" if row.source == "mcp" else "http",
        kind=row.kind,
        default_risk=row.risk_level if row.kind == "change" else "low",
        describe_action=_describe_change(row) if row.kind == "change" else None,
        verify_with=(row.verify_with,) if row.verify_with else (),
        source=row.source,
        input_schema=schema,
        validator=make_validator(schema),
        required_permission=row.required_permission,
        integration_id=row.integration_id,
        definition_id=row.id,
    )
