"""Tenant-registered tools: MCP tool sync with schema pinning, custom HTTP tools,
review/enable/classify, and test runs.

Trust model: everything an MCP server says about a tool (name, description, schema,
annotations) is untrusted. New tools arrive disabled; an administrator reviews and
enables each one and classifies it read or change. The approved schema+description
is pinned by hash: if a later sync sees a different one the tool is disabled again
(``schema_changed``) until it is re-approved — a server cannot silently change what
an approved tool does. Change tools only ever run through the remediation gateway.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.logging import get_logger
from app.database.session import tenant_session
from app.discovery.engine import load_credentials
from app.integrations.http_api import TemplateError, check_template
from app.integrations.mcp import McpClient, McpError, config_from_integration
from app.models import Integration, ToolDefinition
from app.schemas.tooling import (
    HttpToolCreate,
    HttpToolUpdate,
    ParamIn,
    ToolDefinitionOut,
    ToolDefinitionUpdate,
    ToolSyncResult,
)
from app.services import audit_service as audit
from app.services.context import ClientInfo, Principal, RequestContext
from app.tools import catalogue as tool_catalogue
from app.tools.base import ArgumentsError
from app.tools.dynamic import normalise_schema, schema_hash, tool_name
from app.tools.executor import ToolRunResult, run_tool
from app.tools.registry import TOOLS

log = get_logger("localops.tools.registry")

REMEDIATION_PERMISSION = "tool.execute.remediation"


# ------------------------------------------------------------------ helpers


def to_out(row: ToolDefinition, integration_name: str | None = None) -> ToolDefinitionOut:
    out = ToolDefinitionOut.model_validate(row)
    out.integration_name = integration_name
    out.usable = (
        row.enabled and row.status == "active" and row.approved_schema_hash == row.schema_hash
    )
    return out


async def _get(session: AsyncSession, definition_id: uuid.UUID) -> ToolDefinition:
    row = await session.get(ToolDefinition, definition_id)
    if row is None:
        raise NotFoundError("Tool not found")
    return row


async def _integration(session: AsyncSession, integration_id: uuid.UUID, kind: str) -> Integration:
    integration = await session.get(Integration, integration_id)
    if integration is None:
        raise NotFoundError("Integration not found")
    if integration.integration_type != kind:
        label = "an MCP server" if kind == "mcp" else "an HTTP API"
        raise ValidationFailedError(f"Integration '{integration.name}' is not {label}")
    return integration


async def _unique_name(session: AsyncSession, base: str, exclude: uuid.UUID | None = None) -> str:
    taken = set(await session.scalars(select(ToolDefinition.name)))
    if exclude is not None:
        own = await session.get(ToolDefinition, exclude)
        if own is not None:
            taken.discard(own.name)
    name, n = base, 2
    while name in taken or name in TOOLS:
        suffix = f"_{n}"
        name = base[: 60 - len(suffix)] + suffix
        n += 1
    return name


async def list_definitions(
    session: AsyncSession,
    *,
    integration_id: uuid.UUID | None = None,
    source: str | None = None,
    status: str | None = None,
) -> list[ToolDefinitionOut]:
    stmt = select(ToolDefinition, Integration.name).join(
        Integration, Integration.id == ToolDefinition.integration_id
    )
    if integration_id:
        stmt = stmt.where(ToolDefinition.integration_id == integration_id)
    if source:
        stmt = stmt.where(ToolDefinition.source == source)
    if status:
        stmt = stmt.where(ToolDefinition.status == status)
    rows = (await session.execute(stmt.order_by(ToolDefinition.name))).tuples().all()
    return [to_out(row, name) for row, name in rows]


async def get_definition(session: AsyncSession, definition_id: uuid.UUID) -> ToolDefinitionOut:
    row = await _get(session, definition_id)
    integration = await session.get(Integration, row.integration_id)
    return to_out(row, integration.name if integration else None)


async def _check_verify_with(session: AsyncSession, name: str | None) -> None:
    if not name:
        return
    spec = (await tool_catalogue.load(session)).get(name)
    if spec is None or spec.is_change:
        raise ValidationFailedError(f"'{name}' is not an enabled read-only tool")


def _audit_details(row: ToolDefinition, **extra: Any) -> dict[str, Any]:
    return {"tool": row.name, "source": row.source, "kind": row.kind, **extra}


# ------------------------------------------------------------------ MCP sync


async def sync_mcp_tools(
    principal: Principal | None,
    client: ClientInfo | None,
    settings: Settings,
    tenant_id: uuid.UUID,
    integration_id: uuid.UUID,
) -> ToolSyncResult:
    """Fetch ``tools/list`` and reconcile ``tool_definitions`` (no DB held meanwhile)."""
    async with tenant_session(tenant_id) as s:
        integration = await _integration(s, integration_id, "mcp")
        if not integration.enabled:
            raise ConflictError("Integration is disabled")
        config = dict(integration.configuration or {})
        credentials = load_credentials(settings, integration)
        scope = integration.name
    try:
        async with McpClient(
            config_from_integration(config, credentials, settings.mcp_default_timeout_seconds)
        ) as mcp:
            server = dict(mcp.server_info)
            remote_tools = await mcp.list_tools()
    except McpError as exc:
        async with tenant_session(tenant_id) as s:
            integration = await _integration(s, integration_id, "mcp")
            integration.last_status, integration.last_error = "failed", str(exc)[:1000]
            integration.last_run_at = datetime.now(UTC)
        raise ValidationFailedError(f"Tool sync failed: {exc}") from exc

    now = datetime.now(UTC)
    added: list[str] = []
    changed: list[str] = []
    missing: list[str] = []
    unchanged = 0
    async with tenant_session(tenant_id) as s:
        integration = await _integration(s, integration_id, "mcp")
        existing = {
            r.remote_name: r
            for r in await s.scalars(
                select(ToolDefinition).where(ToolDefinition.integration_id == integration_id)
            )
        }
        seen: set[str] = set()
        for tool in remote_tools:
            if tool.name in seen:
                continue
            seen.add(tool.name)
            try:
                schema = normalise_schema(tool.input_schema)
            except ArgumentsError as exc:
                log.warning("mcp_tool_schema_invalid", tool=tool.name, error=str(exc))
                continue
            digest = schema_hash(schema, tool.description)
            row = existing.get(tool.name)
            if row is None:
                read_only = tool.annotations.get("readOnlyHint") is True
                row = ToolDefinition(
                    tenant_id=tenant_id,
                    integration_id=integration_id,
                    source="mcp",
                    name=await _unique_name(s, tool_name("mcp", scope, tool.name)),
                    remote_name=tool.name,
                    title=tool.title,
                    # Admins review the remote text; until then agents never see it.
                    description=tool.description[:1000],
                    remote_description=tool.description,
                    input_schema=schema,
                    output_schema=tool.output_schema,
                    # Untrusted hint: anything not declared read-only starts as "change".
                    kind="read" if read_only else "change",
                    risk_level="low" if read_only else "high",
                    required_permission=(
                        "tool.execute.readonly" if read_only else REMEDIATION_PERMISSION
                    ),
                    timeout_seconds=float(
                        config.get("timeout_seconds") or settings.mcp_default_timeout_seconds
                    ),
                    enabled=False,
                    status="new",
                    schema_hash=digest,
                    last_synced_at=now,
                    created_by=principal.username if principal else "system",
                )
                s.add(row)
                await s.flush()
                added.append(row.name)
                continue
            row.last_synced_at = now
            row.title = tool.title
            if digest == row.schema_hash and row.status != "missing":
                unchanged += 1
                continue
            row.input_schema = schema
            row.output_schema = tool.output_schema
            row.remote_description = tool.description
            row.schema_hash = digest
            if row.approved_schema_hash is None:
                row.status = "new"
            elif row.approved_schema_hash == digest:
                row.status = "active"
            else:
                row.status, row.enabled = "schema_changed", False
            changed.append(row.name)
        for remote, row in existing.items():
            if remote not in seen and row.status != "missing":
                row.status, row.enabled = "missing", False
                missing.append(row.name)
        integration.last_status, integration.last_error = "completed", None
        integration.last_run_at = now
        await audit.record(
            s,
            tenant_id=tenant_id,
            actor=principal,
            username=None if principal else "system",
            action="tool.sync",
            entity_type="integration",
            entity_id=integration_id,
            details={
                "integration": integration.name,
                "total": len(seen),
                "added": added,
                "changed": changed,
                "missing": missing,
            },
            client=client,
        )
    tool_catalogue.invalidate(tenant_id)
    return ToolSyncResult(
        server={k: server.get(k) for k in ("name", "version") if k in server},
        total=len(seen),
        added=added,
        changed=changed,
        missing=missing,
        unchanged=unchanged,
    )


# ------------------------------------------------------------------ review / classify


async def update_definition(
    ctx: RequestContext, definition_id: uuid.UUID, data: ToolDefinitionUpdate
) -> ToolDefinitionOut:
    s, p = ctx.session, ctx.principal
    row = await _get(s, definition_id)
    changes: dict[str, Any] = {}
    if data.approve_schema and row.approved_schema_hash != row.schema_hash:
        if row.status == "missing":
            raise ConflictError("The tool no longer exists on its server")
        row.approved_schema_hash, row.approved_by = row.schema_hash, p.username
        row.status = "active"
        changes["approved_schema"] = row.schema_hash[:12]
    if data.description is not None and data.description != row.description:
        row.description = changes["description"] = data.description.strip()
    if data.kind is not None and data.kind != row.kind:
        row.kind = changes["kind"] = data.kind
    if data.risk_level is not None and data.risk_level != row.risk_level:
        row.risk_level = changes["risk_level"] = data.risk_level
    if data.required_permission is not None and data.required_permission != row.required_permission:
        row.required_permission = changes["required_permission"] = data.required_permission
    if row.kind == "change" and row.required_permission != REMEDIATION_PERMISSION:
        # State-changing tools always need the remediation permission.
        row.required_permission = changes["required_permission"] = REMEDIATION_PERMISSION
    if data.timeout_seconds is not None and data.timeout_seconds != row.timeout_seconds:
        row.timeout_seconds = changes["timeout_seconds"] = data.timeout_seconds
    if data.clear_verify_with and row.verify_with:
        row.verify_with = changes["verify_with"] = None
    elif data.verify_with is not None and data.verify_with != row.verify_with:
        if data.verify_with == row.name:
            raise ValidationFailedError("A tool cannot verify itself")
        await _check_verify_with(s, data.verify_with)
        row.verify_with = changes["verify_with"] = data.verify_with
    if data.enabled is True and not row.enabled:
        if row.status == "missing":
            raise ConflictError("The tool no longer exists on its server")
        if row.status == "schema_changed" or (
            row.approved_schema_hash and row.approved_schema_hash != row.schema_hash
        ):
            raise ConflictError(
                "The tool's schema or description changed since it was approved; review "
                "and approve it first (approve_schema)"
            )
        if row.approved_schema_hash is None:  # enabling a new tool approves what was shown
            row.approved_schema_hash, row.approved_by = row.schema_hash, p.username
            row.status = "active"
            changes["approved_schema"] = row.schema_hash[:12]
        row.enabled = changes["enabled"] = True
    elif data.enabled is False and row.enabled:
        row.enabled = changes["enabled"] = False
    await s.flush()
    if changes:
        await audit.record(
            s,
            tenant_id=p.tenant_id,
            actor=p,
            action="tool.update",
            entity_type="tool",
            entity_id=row.id,
            details=_audit_details(row, changes=changes),
            client=ctx.client,
        )
        tool_catalogue.invalidate(p.tenant_id)
    integration = await s.get(Integration, row.integration_id)
    return to_out(row, integration.name if integration else None)


async def delete_definition(ctx: RequestContext, definition_id: uuid.UUID) -> None:
    s, p = ctx.session, ctx.principal
    row = await _get(s, definition_id)
    details = _audit_details(row)
    await s.delete(row)
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="tool.delete",
        entity_type="tool",
        entity_id=definition_id,
        details=details,
        client=ctx.client,
    )
    tool_catalogue.invalidate(p.tenant_id)


# ------------------------------------------------------------------ HTTP tools


def schema_from_parameters(params: list[ParamIn]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required: list[str] = []
    for prm in params:
        if prm.name in props:
            raise ValidationFailedError(f"Duplicate parameter '{prm.name}'")
        prop: dict[str, Any] = {"type": prm.type}
        if prm.description:
            prop["description"] = prm.description
        if prm.enum:
            prop["enum"] = prm.enum
        if prm.type == "string":
            prop["maxLength"] = prm.max_length or 500
            if prm.pattern:
                prop["pattern"] = prm.pattern
        if prm.type in ("integer", "number"):
            if prm.minimum is not None:
                prop["minimum"] = prm.minimum
            if prm.maximum is not None:
                prop["maximum"] = prm.maximum
        props[prm.name] = prop
        if prm.required:
            required.append(prm.name)
    schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def _http_parts(
    params: list[ParamIn], template: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        schema = normalise_schema(schema_from_parameters(params))
        return schema, check_template(template, schema)
    except (TemplateError, ArgumentsError) as exc:
        raise ValidationFailedError(str(exc)) from exc


async def create_http_tool(ctx: RequestContext, data: HttpToolCreate) -> ToolDefinitionOut:
    s, p = ctx.session, ctx.principal
    integration = await _integration(s, data.integration_id, "http_api")
    schema, template = _http_parts(data.parameters, data.template.model_dump())
    if data.kind == "change" and template["method"] == "GET":
        raise ValidationFailedError("A GET request cannot be a change tool; use POST/PUT/PATCH")
    await _check_verify_with(s, data.verify_with)
    description = data.description.strip()
    digest = schema_hash(schema, description)
    row = ToolDefinition(
        tenant_id=p.tenant_id,
        integration_id=integration.id,
        source="http",
        name=await _unique_name(s, tool_name("http", "", data.name)),
        remote_name=f"{template['method']} {template['path']}"[:200],
        description=description,
        input_schema=schema,
        http_template=template,
        kind=data.kind,
        risk_level=data.risk_level if data.kind == "change" else "low",
        required_permission=(
            REMEDIATION_PERMISSION if data.kind == "change" else "tool.execute.readonly"
        ),
        timeout_seconds=data.timeout_seconds,
        verify_with=data.verify_with,
        enabled=data.enabled,
        status="active",
        schema_hash=digest,
        approved_schema_hash=digest,  # authored by an administrator
        approved_by=p.username,
        created_by=p.username,
    )
    s.add(row)
    await s.flush()
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="tool.create",
        entity_type="tool",
        entity_id=row.id,
        details=_audit_details(row, template=template, integration=integration.name),
        client=ctx.client,
    )
    tool_catalogue.invalidate(p.tenant_id)
    return to_out(row, integration.name)


async def update_http_tool(
    ctx: RequestContext, definition_id: uuid.UUID, data: HttpToolUpdate
) -> ToolDefinitionOut:
    s, p = ctx.session, ctx.principal
    row = await _get(s, definition_id)
    if row.source != "http":
        raise ValidationFailedError("Only custom HTTP tools can be edited this way")
    params = (
        data.parameters if data.parameters is not None else _params_from_schema(row.input_schema)
    )
    template_in = (
        data.template.model_dump() if data.template is not None else dict(row.http_template or {})
    )
    schema, template = _http_parts(params, template_in)
    if row.kind == "change" and template["method"] == "GET":
        raise ValidationFailedError("A GET request cannot be a change tool")
    description = data.description.strip() if data.description else row.description
    row.input_schema, row.http_template, row.description = schema, template, description
    row.remote_name = f"{template['method']} {template['path']}"[:200]
    row.schema_hash = row.approved_schema_hash = schema_hash(schema, description)
    row.approved_by = p.username
    await s.flush()
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="tool.update",
        entity_type="tool",
        entity_id=row.id,
        details=_audit_details(row, template=template),
        client=ctx.client,
    )
    tool_catalogue.invalidate(p.tenant_id)
    integration = await s.get(Integration, row.integration_id)
    return to_out(row, integration.name if integration else None)


def _params_from_schema(schema: dict[str, Any]) -> list[ParamIn]:
    required = set(schema.get("required") or [])
    out = []
    for name, prop in (schema.get("properties") or {}).items():
        out.append(
            ParamIn(
                name=name,
                type=prop.get("type", "string"),
                description=prop.get("description", ""),
                required=name in required,
                enum=prop.get("enum"),
                pattern=prop.get("pattern"),
                max_length=prop.get("maxLength"),
                minimum=prop.get("minimum"),
                maximum=prop.get("maximum"),
            )
        )
    return out


# ------------------------------------------------------------------ test runs


async def test_definition(
    principal: Principal,
    settings: Settings,
    definition_id: uuid.UUID,
    arguments: dict[str, Any],
) -> ToolRunResult:
    """Run an enabled *read* tool once (recorded and audited like any tool run)."""
    async with tenant_session(principal.tenant_id) as s:
        row = await _get(s, definition_id)
        name, kind, enabled = row.name, row.kind, row.enabled
    if kind == "change":
        raise ValidationFailedError(
            "Change tools run only through the remediation workflow (plan, policy, approval)"
        )
    if not enabled:
        raise ConflictError("Enable the tool before testing it")
    tool_catalogue.invalidate(principal.tenant_id)
    start = time.perf_counter()
    result = await run_tool(
        settings,
        principal.tenant_id,
        name,
        arguments,
        allow_remote=True,
        actor=principal.username,
        user_id=principal.user_id,
        permissions=set(principal.permissions),
    )
    log.info(
        "tool_tested", tool=name, status=result.status, ms=int((time.perf_counter() - start) * 1000)
    )
    return result


async def sync_all_mcp(settings: Settings) -> int:
    """Hourly: re-sync every enabled MCP integration (detects schema changes)."""
    from sqlalchemy import text

    from app.database.session import get_engine

    async with get_engine().connect() as conn:
        rows = (
            await conn.execute(text("SELECT id, tenant_id FROM worker_tool_integrations()"))
        ).all()
    done = 0
    for integration_id, tenant_id in rows:
        try:
            await sync_mcp_tools(None, None, settings, tenant_id, integration_id)
            done += 1
        except Exception as exc:  # one unreachable server must not stop the sweep
            log.warning("mcp_sync_failed", integration_id=str(integration_id), error=str(exc)[:300])
    return done
