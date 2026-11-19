"""Remediation policies: defaults, CRUD and evaluation input."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models import Policy, Resource
from app.remediation.policy import (
    DEFAULT_POLICIES,
    Decision,
    PolicyRule,
    PolicyRules,
    evaluate,
)
from app.schemas.remediation import PolicyIn, PolicyOut, PolicyPreviewIn, PolicyUpdate
from app.services import audit_service as audit
from app.services.context import RequestContext
from app.tools.context import resource_info
from app.tools.registry import get_tool


async def ensure_policies(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Seed the built-in policies a tenant lacks (session must be in its context)."""
    existing = set(await session.scalars(select(Policy.name)))
    added = 0
    for spec in DEFAULT_POLICIES:
        if spec["name"] in existing:
            continue
        session.add(
            Policy(
                tenant_id=tenant_id,
                name=spec["name"],
                description=spec["description"],
                priority=spec["priority"],
                rules=PolicyRules.model_validate(spec["rules"]).model_dump(exclude_none=True),
                builtin=True,
            )
        )
        added += 1
    if added:
        await session.flush()
    return added


async def load_rules(session: AsyncSession) -> list[PolicyRule]:
    rows = await session.scalars(select(Policy).where(Policy.enabled.is_(True)))
    out = []
    for p in rows:
        try:
            rules = PolicyRules.model_validate(p.rules)
        except ValueError:
            continue  # a malformed stored rule never widens permissions; skip it
        out.append(PolicyRule(p.name, p.priority, rules, p.enabled))
    return out


async def decide(session: AsyncSession, tool_name: str, resource: Resource, mode: str) -> Decision:
    spec = get_tool(tool_name)
    if spec is None or not spec.is_change:
        raise ValidationFailedError(f"'{tool_name}' is not a remediation tool")
    return evaluate(
        spec,
        resource_info(resource),
        await load_rules(session),
        mode=mode,
        default_ttl=get_settings().approval_default_ttl_minutes,
    )


async def list_policies(ctx: RequestContext) -> list[PolicyOut]:
    rows = await ctx.session.scalars(select(Policy).order_by(Policy.priority, Policy.name))
    return [PolicyOut.model_validate(r) for r in rows]


async def _get(session: AsyncSession, policy_id: uuid.UUID) -> Policy:
    policy = await session.get(Policy, policy_id)
    if policy is None:
        raise NotFoundError("Policy not found")
    return policy


async def _audit(ctx: RequestContext, action: str, policy: Policy, details: dict[str, Any]) -> None:
    await audit.record(
        ctx.session,
        tenant_id=ctx.principal.tenant_id,
        actor=ctx.principal,
        action=action,
        entity_type="policy",
        entity_id=policy.id,
        details={"name": policy.name, **details},
        client=ctx.client,
    )


async def create(ctx: RequestContext, data: PolicyIn) -> PolicyOut:
    policy = Policy(
        tenant_id=ctx.principal.tenant_id,
        name=data.name,
        description=data.description,
        priority=data.priority,
        rules=data.rules.model_dump(exclude_none=True),
        enabled=data.enabled,
    )
    ctx.session.add(policy)
    try:
        async with ctx.session.begin_nested():
            await ctx.session.flush()
    except IntegrityError as exc:
        raise ConflictError("A policy with this name already exists") from exc
    await _audit(ctx, "policy.create", policy, {"rules": policy.rules})
    return PolicyOut.model_validate(policy)


async def update(ctx: RequestContext, policy_id: uuid.UUID, data: PolicyUpdate) -> PolicyOut:
    policy = await _get(ctx.session, policy_id)
    changes: dict[str, Any] = {}
    for field in ("name", "description", "priority", "enabled"):
        value = getattr(data, field)
        if value is not None and value != getattr(policy, field):
            setattr(policy, field, value.strip() if isinstance(value, str) else value)
            changes[field] = value
    if data.rules is not None:
        rules = data.rules.model_dump(exclude_none=True)
        if rules != policy.rules:
            changes["rules"] = {"from": policy.rules, "to": rules}
            policy.rules = rules
    try:
        async with ctx.session.begin_nested():
            await ctx.session.flush()
    except IntegrityError as exc:
        raise ConflictError("A policy with this name already exists") from exc
    if changes:
        await _audit(ctx, "policy.update", policy, {"changes": changes})
    await ctx.session.refresh(policy)
    return PolicyOut.model_validate(policy)


async def delete(ctx: RequestContext, policy_id: uuid.UUID) -> None:
    policy = await _get(ctx.session, policy_id)
    await _audit(ctx, "policy.delete", policy, {"rules": policy.rules})
    await ctx.session.delete(policy)
    await ctx.session.flush()


async def preview(ctx: RequestContext, data: PolicyPreviewIn) -> dict[str, Any]:
    """What the current policies decide for a tool on a resource (for the editor)."""
    resource = await ctx.session.get(Resource, data.resource_id)
    if resource is None:
        raise NotFoundError("Resource not found")
    spec = get_tool(data.tool)
    if spec is None or not spec.is_change:
        raise ValidationFailedError(f"'{data.tool}' is not a remediation tool")
    if resource.resource_type not in spec.resource_types:
        raise ValidationFailedError(f"{data.tool} does not apply to {resource.resource_type}")
    decision = await decide(ctx.session, data.tool, resource, data.execution_mode)
    return {"tool": data.tool, "resource": resource.name, **decision.as_dict()}
