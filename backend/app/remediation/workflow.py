"""Persistence for the remediation steps of an investigation graph.

The graph keeps the plan in its checkpointed state; these functions do the database
work for each step (prepare, request approval, execute through the gateway, record
verification, update the incident). They are idempotent where the graph may re-run a
node after a resume or a crash.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core.config import Settings
from app.database.session import tenant_session
from app.models import Approval, ExecutionJob, Investigation
from app.remediation.gateway import (
    GatewayRejectedError,
    PreparedAction,
    execute_approved,
    prepare,
)
from app.repositories.identity import user_permissions
from app.services import approval_service, incident_service, policy_service


async def context(
    settings: Settings, tenant_id: uuid.UUID, investigation_id: uuid.UUID
) -> dict[str, Any]:
    async with tenant_session(tenant_id) as s:
        inv = await s.get(Investigation, investigation_id)
        if inv is None:
            return {"requester_id": None, "can_remediate": False, "mode": "assisted"}
        perms = await user_permissions(s, inv.user_id)
        return {
            "requester_id": str(inv.user_id),
            "can_remediate": "tool.execute.remediation" in perms,
            "mode": inv.execution_mode,
            "enabled": settings.remediation_enabled,
        }


def step_from(prepared: PreparedAction, index: int, extra: dict[str, Any]) -> dict[str, Any]:
    decision = prepared.decision
    return {
        "index": index,
        "tool": prepared.spec.name,
        "arguments": prepared.arguments,
        "resource_id": str(prepared.resource.id),
        "resource_name": prepared.resource.name,
        "resource_type": prepared.resource.resource_type,
        "description": prepared.description,
        "risk_level": decision.risk_level,
        "policy": decision.as_dict(),
        "verify_with": list(prepared.spec.verify_with),
        "status": "blocked" if decision.denied else "proposed",
        "status_reason": decision.denied_reason if decision.denied else None,
        "approval_id": None,
        "job_id": None,
        "execution": None,
        "verification": None,
        "revisions": 0,
        **extra,
    }


async def prepare_step(
    settings: Settings,
    tenant_id: uuid.UUID,
    tool: str,
    arguments: dict[str, Any],
    mode: str,
) -> PreparedAction:
    async with tenant_session(tenant_id) as s:
        return await prepare(
            s,
            tool,
            arguments,
            mode=mode,
            rules=await policy_service.load_rules(s),
            default_ttl=settings.approval_default_ttl_minutes,
        )


async def ensure_approval(
    settings: Settings,
    tenant_id: uuid.UUID,
    investigation_id: uuid.UUID,
    step: dict[str, Any],
    requested_by: uuid.UUID,
) -> dict[str, Any]:
    """The approval for this exact step, created on first call (idempotent)."""
    async with tenant_session(tenant_id) as s:
        inv = await s.get(Investigation, investigation_id)
        if inv is None:
            raise GatewayRejectedError("Investigation not found", "not_found")
        prepared = await prepare(
            s,
            step["tool"],
            step["arguments"],
            mode=inv.execution_mode,
            rules=await policy_service.load_rules(s),
            default_ttl=settings.approval_default_ttl_minutes,
        )
        digest = prepared.hash_for(inv.id)
        existing = await s.scalar(
            select(Approval)
            .where(
                Approval.investigation_id == inv.id,
                Approval.step_index == step["index"],
                Approval.action_hash == digest,
                Approval.decision_comment.is_distinct_from("Superseded by a revised request"),
            )
            .order_by(Approval.created_at.desc())
            .limit(1)
        )
        if existing is not None:
            return {**_approval_dict(existing), "created": False}
        if prepared.decision.denied:
            raise GatewayRejectedError(
                prepared.decision.denied_reason or "Blocked by policy", "policy_denied"
            )
        approval = await approval_service.create_for_step(
            s,
            tenant_id=tenant_id,
            investigation=inv,
            step_index=step["index"],
            prepared=prepared,
            rationale=step.get("rationale", ""),
            expected_impact=step.get("expected_impact", ""),
            evidence_ids=step.get("evidence_ids", []),
            evidence=step.get("evidence", []),
            requested_by=requested_by,
        )
        if approval.auto_approved:
            summary = f"Auto-approved by policy: {approval.description}"
            await incident_service.workflow_update(
                s,
                inv.incident_id,
                "approval_auto",
                summary[:1000],
                details={"approval_id": str(approval.id), "risk_level": approval.risk_level},
            )
        else:
            await incident_service.workflow_update(
                s,
                inv.incident_id,
                "approval_requested",
                f"Approval requested ({approval.risk_level} risk): {approval.description}"[:1000],
                status="awaiting_approval",
                details={"approval_id": str(approval.id), "risk_level": approval.risk_level},
            )
        return {**_approval_dict(approval), "created": True}


def _approval_dict(a: Approval) -> dict[str, Any]:
    return {
        "approval_id": str(a.id),
        "status": a.status,
        "auto_approved": a.auto_approved,
        "comment": a.decision_comment,
        "risk_level": a.risk_level,
        "expires_at": a.expires_at.isoformat(),
        "approved_by": str(a.approved_by) if a.approved_by else None,
    }


async def approval_info(tenant_id: uuid.UUID, approval_id: str) -> dict[str, Any]:
    async with tenant_session(tenant_id) as s:
        a = await s.get(Approval, uuid.UUID(approval_id))
        if a is None:
            return {"approval_id": approval_id, "status": "cancelled", "comment": "missing"}
        if a.status == "pending" and a.expires_at < datetime.now(UTC):
            a.status = "expired"
            a.decided_at = datetime.now(UTC)
        return _approval_dict(a)


async def execute(
    settings: Settings,
    tenant_id: uuid.UUID,
    approval_id: str,
    before: dict[str, Any],
) -> dict[str, Any]:
    try:
        result = await execute_approved(
            settings,
            tenant_id,
            uuid.UUID(approval_id),
            rules_loader=policy_service.load_rules,
            before=before,
        )
    except GatewayRejectedError as exc:
        return {"job_id": None, "status": "rejected", "summary": str(exc), "error_code": exc.code}
    out = result.as_dict()
    out["data"] = {}
    async with tenant_session(tenant_id) as s:
        job = await s.get(ExecutionJob, uuid.UUID(result.job_id))
        if job is not None:
            out["data"] = job.execution_result.get("data") or {}
            out["output_excerpt"] = str(job.execution_result.get("output") or "")[:1500]
    return out


async def record_verification(
    tenant_id: uuid.UUID,
    job_id: str | None,
    after: dict[str, Any],
    verdict: dict[str, Any],
) -> None:
    if not job_id:
        return
    async with tenant_session(tenant_id) as s:
        job = await s.get(ExecutionJob, uuid.UUID(job_id))
        if job is None:
            return
        job.after = after
        job.verification = verdict
        job.verification_status = verdict.get("status", "failed")


async def incident_step(
    tenant_id: uuid.UUID,
    investigation_id: uuid.UUID,
    event_type: str,
    summary: str,
    *,
    status: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    async with tenant_session(tenant_id) as s:
        inv = await s.get(Investigation, investigation_id)
        if inv is None or inv.incident_id is None:
            return
        await incident_service.workflow_update(
            s, inv.incident_id, event_type, summary[:1000], status=status, details=details
        )
