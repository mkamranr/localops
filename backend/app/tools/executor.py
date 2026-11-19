"""Run a tool: validate, resolve the resource, enforce policy, execute, redact, record.

This is the single path by which agents and users run diagnostic tools. Tool calls
proposed by a model are untrusted input: unknown tools, invalid arguments, resources
of the wrong type or remote tools without permission are *rejected* with a message
the model can act on, and recorded.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import func, or_, select

from app.core.config import Settings
from app.database.session import tenant_session
from app.models import Resource, ToolExecution
from app.services import audit_service as audit
from app.tools.base import ResourceInfo, ToolError, ToolSpec
from app.tools.context import ToolContext, resource_info
from app.tools.redaction import cap, redact
from app.tools.registry import TOOLS

log = structlog.get_logger("localops.tools")

STORED_OUTPUT_BYTES = 16_000


@dataclass(slots=True)
class ToolRunResult:
    tool: str
    status: str  # success | failed | rejected
    summary: str
    model_text: str  # what a model may read (redacted, capped)
    arguments: dict[str, Any] = field(default_factory=dict)
    resource_id: str | None = None
    resource_name: str | None = None
    resource_type: str | None = None
    error: str | None = None
    error_code: str | None = None
    duration_ms: int = 0
    execution_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "success"


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:6]:
        loc = ".".join(str(p) for p in err["loc"]) or "arguments"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def _clean_args(values: dict[str, Any]) -> dict[str, Any]:
    return {k: (redact(v) if isinstance(v, str) else v) for k, v in values.items()}


async def _find_resource(tenant_id: uuid.UUID, ref: str) -> ResourceInfo | None:
    async with tenant_session(tenant_id) as session:
        resource: Resource | None = None
        try:
            resource = await session.get(Resource, uuid.UUID(ref))
        except ValueError:
            lowered = ref.strip().lower()
            rows = list(
                await session.scalars(
                    select(Resource)
                    .where(
                        or_(
                            func.lower(Resource.name) == lowered,
                            func.lower(Resource.hostname) == lowered,
                        )
                    )
                    .limit(2)
                )
            )
            resource = rows[0] if len(rows) == 1 else None
        return resource_info(resource) if resource else None


def tool_catalogue_text(specs: list[ToolSpec]) -> str:
    return ", ".join(s.name for s in specs)


async def run_tool(
    settings: Settings,
    tenant_id: uuid.UUID,
    name: str,
    raw_args: dict[str, Any] | None,
    *,
    allow_remote: bool,
    allowed: list[ToolSpec] | None = None,
    investigation_id: uuid.UUID | None = None,
    agent_type: str | None = None,
    actor: str | None = None,
    user_id: uuid.UUID | None = None,
    evidence_id: str | None = None,
) -> ToolRunResult:
    start = time.perf_counter()
    spec = TOOLS.get(name)
    allowed_names = {s.name for s in allowed} if allowed is not None else None
    raw_args = dict(raw_args or {})

    def reject(message: str, code: str, **extra: Any) -> ToolRunResult:
        return ToolRunResult(
            tool=name,
            status="rejected",
            summary=f"Rejected: {message}",
            model_text=f"ERROR: {message}",
            arguments=_clean_args(raw_args),
            error=message,
            error_code=code,
            **extra,
        )

    result: ToolRunResult
    resource: ResourceInfo | None = None
    if spec is None or (allowed_names is not None and name not in allowed_names):
        choices = ", ".join(sorted(allowed_names or TOOLS))
        result = reject(
            f"Unknown or unavailable tool '{name}'. Available: {choices}", "unknown_tool"
        )
    else:
        try:
            args = spec.args.model_validate(raw_args)
        except ValidationError as exc:
            result = reject(f"Invalid arguments: {_validation_message(exc)}", "invalid_arguments")
        else:
            ref = getattr(args, "resource_id", None)
            if ref:
                resource = await _find_resource(tenant_id, str(ref))
            if spec.remote and not allow_remote:
                result = reject(
                    "This investigation may not run remote diagnostic tools (the requesting "
                    "user lacks tool.execute.readonly). Use inventory tools instead.",
                    "not_permitted",
                )
            elif ref and resource is None:
                result = reject(
                    f"Resource '{ref}' was not found. Use a resource_id from the list provided.",
                    "unknown_resource",
                )
            elif (
                spec.needs_resource
                and resource
                and resource.resource_type not in spec.resource_types
            ):
                fitting = sorted(
                    t.name
                    for t in (allowed or TOOLS.values())
                    if resource.resource_type in t.resource_types
                )
                result = reject(
                    f"{spec.name} does not apply to {resource.name} "
                    f"({resource.resource_type}). Tools for it: {', '.join(fitting)}",
                    "wrong_resource_type",
                )
            else:
                result = await _execute(settings, tenant_id, spec, args, resource, investigation_id)
    if resource is not None:
        result.resource_id = str(resource.id)
        result.resource_name = resource.name
        result.resource_type = resource.resource_type
    result.duration_ms = int((time.perf_counter() - start) * 1000)
    await _record(tenant_id, result, investigation_id, agent_type, actor, user_id, evidence_id)
    return result


async def _execute(
    settings: Settings,
    tenant_id: uuid.UUID,
    spec: ToolSpec,
    args: Any,
    resource: ResourceInfo | None,
    investigation_id: uuid.UUID | None,
) -> ToolRunResult:
    ctx = ToolContext(
        settings=settings,
        tenant_id=tenant_id,
        resource=resource,
        timeout=spec.timeout_seconds,
        investigation_id=investigation_id,
    )
    arguments = _clean_args(args.model_dump(mode="json"))
    try:
        output = await asyncio.wait_for(spec.run(ctx, args), spec.timeout_seconds + 15)
    except ToolError as exc:
        message = redact(str(exc))
        return ToolRunResult(
            tool=spec.name,
            status="failed",
            summary=f"{spec.name} failed: {message[:200]}",
            model_text=f"ERROR ({exc.code}): {message}",
            arguments=arguments,
            error=message,
            error_code=exc.code,
        )
    except TimeoutError:
        return ToolRunResult(
            tool=spec.name,
            status="failed",
            summary=f"{spec.name} timed out",
            model_text="ERROR (timeout): the tool did not finish in time",
            arguments=arguments,
            error="timed out",
            error_code="timeout",
        )
    except Exception as exc:  # a tool bug must not sink an investigation
        log.warning("tool_crashed", tool=spec.name, error=type(exc).__name__)
        return ToolRunResult(
            tool=spec.name,
            status="failed",
            summary=f"{spec.name} failed unexpectedly ({type(exc).__name__})",
            model_text=f"ERROR: internal tool error ({type(exc).__name__})",
            arguments=arguments,
            error=f"internal error: {type(exc).__name__}",
            error_code="internal_error",
        )
    text = redact(output.text)
    return ToolRunResult(
        tool=spec.name,
        status="success",
        summary=redact(output.summary)[:500],
        model_text=cap(text, settings.tool_output_model_bytes),
        arguments=arguments,
        data={"stored": cap(text, STORED_OUTPUT_BYTES), **output.data},
    )


async def _record(
    tenant_id: uuid.UUID,
    result: ToolRunResult,
    investigation_id: uuid.UUID | None,
    agent_type: str | None,
    actor: str | None,
    user_id: uuid.UUID | None,
    evidence_id: str | None,
) -> None:
    stored = result.data.pop("stored", None)
    async with tenant_session(tenant_id) as session:
        row = ToolExecution(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            resource_id=uuid.UUID(result.resource_id) if result.resource_id else None,
            resource_name=result.resource_name,
            agent_type=agent_type,
            actor=actor,
            evidence_id=evidence_id if result.status != "rejected" else None,
            tool=result.tool[:60],
            arguments=result.arguments,
            status=result.status,
            duration_ms=result.duration_ms,
            output=stored if stored is not None else (result.model_text if not result.ok else None),
            summary=result.summary[:500],
            error=result.error[:1000] if result.error else None,
        )
        session.add(row)
        await session.flush()
        result.execution_id = str(row.id)
        await audit.record(
            session,
            tenant_id=tenant_id,
            action="tool.execute",
            user_id=user_id,
            username=actor or (f"agent:{agent_type}" if agent_type else None),
            outcome="success" if result.ok else "failure",
            entity_type="resource" if result.resource_id else None,
            entity_id=uuid.UUID(result.resource_id) if result.resource_id else None,
            details={
                "tool": result.tool,
                "status": result.status,
                "agent": agent_type,
                "investigation_id": str(investigation_id) if investigation_id else None,
                "arguments": result.arguments,
                "error_code": result.error_code,
            },
        )
