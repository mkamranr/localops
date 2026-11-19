"""Remediation policy engine: risk, approval and autonomy decisions.

A policy rule is ``{"match": {...}, "effect": {...}}``. Rules are applied in ascending
``priority``; every matching rule's effect overrides what earlier rules decided.

``match`` (all keys optional; a missing or empty list matches anything):
    tools, categories, risks (the risk decided so far), environments, resource_types,
    tags (any of), groups.
    ``environments`` may contain ``"*nonprod*"`` meaning "set, and not production".

``effect`` (all keys optional):
    risk_level, auto_approve, allow_self_approval, deny (+ deny_reason),
    approval_ttl_minutes.

Fixed rules that no policy can override:
    * critical actions are never auto-approved and never self-approved;
    * outside ``autonomous`` mode every action needs a human approval;
    * a denied action cannot run at all.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.remediation import RISK_LEVELS
from app.tools.base import ResourceInfo, ToolSpec

PRODUCTION = frozenset({"production", "prod", "prd", "live"})
NONPROD = "*nonprod*"
DEFAULT_TTL_MINUTES = 60


class PolicyMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools: list[str] = Field(default_factory=list, max_length=50)
    categories: list[str] = Field(default_factory=list, max_length=10)
    risks: list[str] = Field(default_factory=list, max_length=4)
    environments: list[str] = Field(default_factory=list, max_length=20)
    resource_types: list[str] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=20)
    groups: list[str] = Field(default_factory=list, max_length=20)


class PolicyEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_level: str | None = Field(None, pattern="^(low|medium|high|critical)$")
    auto_approve: bool | None = None
    allow_self_approval: bool | None = None
    deny: bool | None = None
    deny_reason: str | None = Field(None, max_length=300)
    approval_ttl_minutes: int | None = Field(None, ge=5, le=10080)


class PolicyRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match: PolicyMatch = Field(default_factory=PolicyMatch)
    effect: PolicyEffect = Field(default_factory=PolicyEffect)


@dataclass(slots=True)
class PolicyRule:
    name: str
    priority: int
    rules: PolicyRules
    enabled: bool = True


@dataclass(slots=True)
class Decision:
    risk_level: str
    requires_approval: bool
    auto_approve: bool  # policy allows running without a human (autonomous mode only)
    allow_self_approval: bool
    approval_ttl_minutes: int
    denied: bool = False
    denied_reason: str | None = None
    matched_rules: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _norm(values: list[str]) -> set[str]:
    return {v.strip().lower() for v in values if v.strip()}


def is_production(environment: str | None) -> bool:
    return (environment or "").strip().lower() in PRODUCTION


def matches(m: PolicyMatch, tool: ToolSpec, resource: ResourceInfo, risk: str) -> bool:
    if m.tools and tool.name not in m.tools:
        return False
    if m.categories and tool.category.lower() not in _norm(m.categories):
        return False
    if m.risks and risk not in _norm(m.risks):
        return False
    if m.environments:
        wanted = _norm(m.environments)
        env = (resource.environment or "").strip().lower()
        # "*nonprod*" needs an explicit environment: an untagged resource may be production.
        nonprod_ok = NONPROD in wanted and bool(env) and not is_production(env)
        if env not in wanted and not nonprod_ok:
            return False
    if m.resource_types and resource.resource_type not in m.resource_types:
        return False
    if m.tags and not (_norm(m.tags) & _norm(resource.tags)):
        return False
    return not (m.groups and (resource.group_name or "").lower() not in _norm(m.groups))


def evaluate(
    tool: ToolSpec,
    resource: ResourceInfo,
    rules: list[PolicyRule],
    *,
    mode: str,
    default_ttl: int = DEFAULT_TTL_MINUTES,
) -> Decision:
    """Decide risk, approval and autonomy for running ``tool`` on ``resource``."""
    risk = tool.default_risk if tool.default_risk in RISK_LEVELS else "high"
    auto = False
    self_ok = False
    ttl = default_ttl
    denied = False
    denied_reason: str | None = None
    matched: list[str] = []
    for rule in sorted(rules, key=lambda r: (r.priority, r.name)):
        if not rule.enabled or not matches(rule.rules.match, tool, resource, risk):
            continue
        matched.append(rule.name)
        e = rule.rules.effect
        if e.risk_level:
            risk = e.risk_level
        if e.auto_approve is not None:
            auto = e.auto_approve
        if e.allow_self_approval is not None:
            self_ok = e.allow_self_approval
        if e.approval_ttl_minutes:
            ttl = e.approval_ttl_minutes
        if e.deny:
            denied = True
            denied_reason = e.deny_reason or f"Blocked by policy '{rule.name}'"

    notes: list[str] = []
    if risk == "critical":
        if auto:
            notes.append("Critical actions always need a human approval.")
        if self_ok:
            notes.append("Critical actions always need a second person to approve.")
        auto = False
        self_ok = False
    if auto and mode != "autonomous":
        notes.append("Auto-approval applies only in autonomous mode.")
    runs_unattended = auto and mode == "autonomous" and not denied
    return Decision(
        risk_level=risk,
        requires_approval=not runs_unattended,
        auto_approve=auto,
        allow_self_approval=self_ok,
        approval_ttl_minutes=ttl,
        denied=denied,
        denied_reason=denied_reason,
        matched_rules=matched,
        notes=notes,
    )


# Seeded for every tenant; editable under Settings → Policies.
DEFAULT_POLICIES: tuple[dict[str, Any], ...] = (
    {
        "name": "Production changes are high risk",
        "description": "Every change on a production resource is at least high risk.",
        "priority": 10,
        "rules": {
            "match": {"environments": sorted(PRODUCTION), "risks": ["low", "medium"]},
            "effect": {"risk_level": "high"},
        },
    },
    {
        "name": "Non-production container restarts are low risk",
        "description": (
            "Restarting or starting a container outside production is low risk and may run "
            "unattended in autonomous mode."
        ),
        "priority": 20,
        "rules": {
            "match": {
                "tools": ["docker_container_restart", "docker_container_start"],
                "environments": [NONPROD],
            },
            "effect": {"risk_level": "low", "auto_approve": True},
        },
    },
    {
        "name": "Reboots are critical",
        "description": "Rebooting a server is critical and always needs a second approver.",
        "priority": 90,
        "rules": {
            "match": {"tools": ["linux_reboot", "windows_reboot"]},
            "effect": {"risk_level": "critical", "approval_ttl_minutes": 30},
        },
    },
)
