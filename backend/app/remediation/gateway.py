"""Tool execution gateway: the only path by which a state-changing tool runs (spec §8.4).

``prepare`` validates a proposed action (registered change tool, strict arguments, an
existing resource of the right type) and evaluates policy. ``execute_approved`` runs
an action whose approval is valid *at execution time*:

1. remediation is enabled (``REMEDIATION_ENABLED`` kill switch);
2. the approval is ``approved`` and was decided recently enough;
3. the action, re-validated from the stored parameters, still has the approved hash;
4. the requester is active and holds ``tool.execute.remediation``;
5. a human approver is active, holds ``approval.approve`` and is not the requester
   (unless the policy allowed self-approval, never for critical actions); an
   auto-approval is valid only while the policy still allows unattended execution;
6. policy re-evaluated now does not deny the action or raise its risk;
7. the approval has not executed before (one job per approval, enforced by a unique key).

Every outcome, including refusals, is an ``execution_jobs`` row and an audit record.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.database.session import tenant_session
from app.models import Approval, ExecutionJob, Investigation, Resource, User
from app.remediation.policy import Decision, PolicyRule, evaluate
from app.repositories.identity import user_permissions
from app.services import audit_service as audit
from app.tools.base import ResourceInfo, ToolSpec
from app.tools.context import resource_info
from app.tools.executor import _execute
from app.tools.redaction import redact
from app.tools.registry import get_tool

log = structlog.get_logger("localops.remediation")

RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class GatewayRejectedError(Exception):
    """The action may not run; ``code`` is machine-readable."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class PreparedAction:
    spec: ToolSpec
    args: BaseModel
    arguments: dict[str, Any]  # canonical, validated
    resource: ResourceInfo
    description: str
    decision: Decision

    def hash_for(self, investigation_id: uuid.UUID | str) -> str:
        return action_hash(self.spec.name, self.arguments, self.resource.id, investigation_id)


def action_hash(
    tool: str,
    arguments: dict[str, Any],
    resource_id: uuid.UUID | str,
    investigation_id: uuid.UUID | str,
) -> str:
    """Binds an approval to one exact action: any change yields a different hash."""
    payload = json.dumps(
        {
            "tool": tool,
            "arguments": arguments,
            "resource_id": str(resource_id),
            "investigation_id": str(investigation_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _errors(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:6]:
        loc = ".".join(str(p) for p in err["loc"]) or "arguments"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


async def _resource(session: AsyncSession, ref: str) -> Resource | None:
    try:
        return await session.get(Resource, uuid.UUID(str(ref)))
    except ValueError:
        lowered = str(ref).strip().lower()
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
        return rows[0] if len(rows) == 1 else None


async def prepare(
    session: AsyncSession,
    tool: str,
    raw_args: dict[str, Any] | None,
    *,
    mode: str,
    rules: list[PolicyRule],
    default_ttl: int,
) -> PreparedAction:
    """Validate a proposed action and decide its policy (session in tenant context)."""
    spec = get_tool(tool)
    if spec is None or not spec.is_change:
        raise GatewayRejectedError(f"'{tool}' is not a registered remediation tool", "unknown_tool")
    raw = dict(raw_args or {})
    try:
        args = spec.args.model_validate(raw)
    except ValidationError as exc:
        raise GatewayRejectedError(
            f"Invalid arguments: {_errors(exc)}", "invalid_arguments"
        ) from exc
    resource = await _resource(session, str(getattr(args, "resource_id", "")))
    if resource is None:
        raise GatewayRejectedError(
            f"Resource '{getattr(args, 'resource_id', '')}' was not found", "unknown_resource"
        )
    if resource.resource_type not in spec.resource_types:
        raise GatewayRejectedError(
            f"{spec.name} does not apply to {resource.name} ({resource.resource_type})",
            "wrong_resource_type",
        )
    # Canonical form: the resource is always referenced by id.
    args = spec.args.model_validate({**args.model_dump(), "resource_id": str(resource.id)})
    info = resource_info(resource)
    decision = evaluate(spec, info, rules, mode=mode, default_ttl=default_ttl)
    describe = spec.describe_action
    description = describe(args, info) if describe else f"Run {spec.name} on {resource.name}"
    return PreparedAction(
        spec=spec,
        args=args,
        arguments=args.model_dump(mode="json"),
        resource=info,
        description=redact(description)[:1000],
        decision=decision,
    )


# ------------------------------------------------------------------ execution


@dataclass(slots=True)
class ExecutionResult:
    job_id: str
    status: str  # succeeded | failed | rejected
    summary: str
    error: str | None = None
    error_code: str | None = None
    output: str | None = None
    duplicate: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "summary": self.summary,
            "error": self.error,
            "error_code": self.error_code,
            "duplicate": self.duplicate,
        }


async def _active_with(
    session: AsyncSession, user_id: uuid.UUID | None, permission: str
) -> tuple[bool, str | None]:
    if user_id is None:
        return False, None
    user = await session.get(User, user_id)
    if user is None or user.status != "active":
        return False, user.username if user else None
    return permission in await user_permissions(session, user_id), user.username


async def _check(
    session: AsyncSession,
    settings: Settings,
    approval: Approval,
    inv: Investigation,
    rules: list[PolicyRule],
) -> PreparedAction:
    """Re-validate everything about an approved action; raises when it may not run."""
    if not settings.remediation_enabled:
        raise GatewayRejectedError(
            "Remediation is disabled on this installation (REMEDIATION_ENABLED=false)",
            "remediation_disabled",
        )
    if approval.status != "approved":
        raise GatewayRejectedError(f"The approval is {approval.status}", "not_approved")
    ttl = int(approval.policy.get("approval_ttl_minutes") or settings.approval_default_ttl_minutes)
    if approval.decided_at and datetime.now(UTC) > approval.decided_at + timedelta(minutes=ttl):
        raise GatewayRejectedError(
            "The approval is too old to execute; request a new one", "approval_stale"
        )
    prepared = await prepare(
        session,
        approval.action,
        approval.action_parameters,
        mode=inv.execution_mode,
        rules=rules,
        default_ttl=settings.approval_default_ttl_minutes,
    )
    if prepared.hash_for(inv.id) != approval.action_hash:
        raise GatewayRejectedError(
            "The action differs from what was approved; a new approval is required",
            "approval_mismatch",
        )
    ok, _name = await _active_with(session, approval.requested_by, "tool.execute.remediation")
    if not ok:
        raise GatewayRejectedError(
            "The requesting user may not run remediation tools (tool.execute.remediation)",
            "not_permitted",
        )
    decision = prepared.decision
    if decision.denied:
        raise GatewayRejectedError(decision.denied_reason or "Blocked by policy", "policy_denied")
    if RISK_ORDER[decision.risk_level] > RISK_ORDER.get(approval.risk_level, 3):
        raise GatewayRejectedError(
            f"Policy now rates this action {decision.risk_level} (approved as "
            f"{approval.risk_level}); a new approval is required",
            "policy_changed",
        )
    if approval.auto_approved:
        if decision.requires_approval:
            raise GatewayRejectedError(
                "Policy no longer allows this action to run without approval", "policy_changed"
            )
    else:
        ok, _name = await _active_with(session, approval.approved_by, "approval.approve")
        if not ok:
            raise GatewayRejectedError(
                "The approver is no longer active or may no longer approve", "approver_invalid"
            )
        self_ok = bool(approval.policy.get("allow_self_approval")) and (
            decision.risk_level != "critical"
        )
        if approval.approved_by == approval.requested_by and not self_ok:
            raise GatewayRejectedError(
                "The requester may not approve their own action", "self_approval"
            )
    return prepared


async def execute_approved(
    settings: Settings,
    tenant_id: uuid.UUID,
    approval_id: uuid.UUID,
    *,
    rules_loader: Any,
    before: dict[str, Any] | None = None,
) -> ExecutionResult:
    """Run the approved action once. Safe to call again: a second call returns the
    recorded outcome without executing."""
    start = time.perf_counter()
    async with tenant_session(tenant_id) as session:
        approval = await session.get(Approval, approval_id, with_for_update=True)
        if approval is None:
            raise GatewayRejectedError("Approval not found", "not_found")
        existing = await session.scalar(
            select(ExecutionJob).where(ExecutionJob.approval_id == approval.id)
        )
        if existing is not None:
            return ExecutionResult(
                job_id=str(existing.id),
                status=existing.status,
                summary=str(existing.execution_result.get("summary") or existing.status),
                error=existing.execution_result.get("error"),
                error_code=existing.execution_result.get("error_code"),
                duplicate=True,
            )
        inv = await session.get(Investigation, approval.investigation_id)
        if inv is None:
            raise GatewayRejectedError("Investigation not found", "not_found")
        job = ExecutionJob(
            tenant_id=tenant_id,
            investigation_id=approval.investigation_id,
            approval_id=approval.id,
            step_index=approval.step_index,
            tool=approval.action,
            resource_id=approval.resource_id,
            resource_name=approval.resource_name,
            parameters=dict(approval.action_parameters),
            description=approval.description,
            risk_level=approval.risk_level,
            actor="policy (autonomous)" if approval.auto_approved else "gateway",
            status="running",
            before=before or {},
        )
        rejection: GatewayRejectedError | None = None
        prepared: PreparedAction | None = None
        try:
            prepared = await _check(session, settings, approval, inv, await rules_loader(session))
        except GatewayRejectedError as exc:
            rejection = exc
        if rejection is not None:
            job.status = "rejected"
            job.verification_status = "skipped"
            job.completed_at = datetime.now(UTC)
            job.execution_result = {
                "summary": f"Refused: {rejection}",
                "error": str(rejection),
                "error_code": rejection.code,
            }
            approval.status = "failed"
        session.add(job)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:  # a concurrent call created the job first
            await session.rollback()
            return await execute_approved(
                settings, tenant_id, approval_id, rules_loader=rules_loader, before=before
            )
        job_id = job.id
        if rejection is not None:
            await _audit(session, tenant_id, approval, job, "failure", rejection.code)
            return ExecutionResult(
                job_id=str(job_id),
                status="rejected",
                summary=f"Refused: {rejection}",
                error=str(rejection),
                error_code=rejection.code,
            )

    if prepared is None:  # unreachable: rejected above, or prepared
        raise GatewayRejectedError("The action could not be prepared", "internal_error")
    # Execute outside any transaction: the job row already marks the attempt.
    result = await _execute(
        settings,
        tenant_id,
        prepared.spec,
        prepared.args,
        prepared.resource,
        approval.investigation_id,
    )
    stored = result.data.pop("stored", None)
    duration = int((time.perf_counter() - start) * 1000)
    status = "succeeded" if result.ok else "failed"
    async with tenant_session(tenant_id) as session:
        done = await session.get(ExecutionJob, job_id)
        approval = await session.get(Approval, approval_id)
        if done is None or approval is None:
            raise GatewayRejectedError("The execution record disappeared", "internal_error")
        job = done
        job.status = status
        job.completed_at = datetime.now(UTC)
        job.duration_ms = duration
        job.execution_result = {
            "summary": result.summary,
            "output": stored if stored is not None else result.model_text,
            "error": result.error,
            "error_code": result.error_code,
            "data": {k: v for k, v in result.data.items() if k != "stored"},
        }
        if not result.ok:
            job.verification_status = "skipped"
        approval.status = "executed" if result.ok else "failed"
        await _audit(session, tenant_id, approval, job, "success" if result.ok else "failure")
    log.info(
        "remediation_executed",
        tool=approval.action,
        status=status,
        investigation_id=str(approval.investigation_id),
    )
    return ExecutionResult(
        job_id=str(job_id),
        status=status,
        summary=result.summary,
        error=result.error,
        error_code=result.error_code,
        output=stored,
    )


async def _audit(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    approval: Approval,
    job: ExecutionJob,
    outcome: str,
    code: str | None = None,
) -> None:
    await audit.record(
        session,
        tenant_id=tenant_id,
        action="remediation.execute",
        user_id=approval.approved_by,
        username="policy" if approval.auto_approved else None,
        outcome=outcome,
        entity_type="resource" if approval.resource_id else None,
        entity_id=approval.resource_id,
        details={
            "tool": approval.action,
            "arguments": approval.action_parameters,
            "description": approval.description,
            "risk_level": approval.risk_level,
            "approval_id": str(approval.id),
            "auto_approved": approval.auto_approved,
            "requested_by": str(approval.requested_by),
            "approved_by": str(approval.approved_by) if approval.approved_by else None,
            "investigation_id": str(approval.investigation_id),
            "job_id": str(job.id),
            "status": job.status,
            "error_code": code or job.execution_result.get("error_code"),
        },
    )
