"""Built-in agent catalogue (spec §11).

Every tenant gets one row per catalogue entry in ``agents`` so administrators can
enable/disable agents and choose their model. ``available_in_phase`` marks agents whose
implementation lands in a later delivery phase; the supervisor may still *plan* work
for them, but the dispatcher reports those tasks as skipped instead of inventing output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

IMPLEMENTED_PHASE: Final = 5

DEFAULT_AGENT_CONFIG: Final[dict[str, Any]] = {
    "temperature": 0.1,
    "max_tokens": 1024,
    "timeout_seconds": 120,
    "max_retries": 2,
    "allowed_tools": [],
}


@dataclass(frozen=True, slots=True)
class AgentSpec:
    agent_type: str
    name: str
    description: str
    available_in_phase: int
    specialist: bool = True
    capabilities: tuple[str, ...] = field(default_factory=tuple)

    @property
    def implemented(self) -> bool:
        return self.available_in_phase <= IMPLEMENTED_PHASE


CATALOGUE: Final[tuple[AgentSpec, ...]] = (
    AgentSpec(
        "supervisor",
        "Supervisor Agent",
        "Classifies requests, identifies affected entities, plans investigation tasks, "
        "dispatches specialist agents and assembles the final report.",
        2,
        specialist=False,
        capabilities=("classification", "planning", "coordination", "reporting"),
    ),
    AgentSpec(
        "discovery",
        "Discovery Agent",
        "Resolves affected systems against the infrastructure inventory and reports their "
        "health, key facts and dependencies as evidence.",
        3,
        capabilities=("inventory", "topology"),
    ),
    AgentSpec(
        "incident_triage",
        "Incident Triage Agent",
        "Assesses severity and priority of an incident from health, environment, blast "
        "radius and other open incidents, and suggests investigation paths.",
        4,
        capabilities=("severity", "impact"),
    ),
    AgentSpec(
        "infrastructure_diagnostics",
        "Infrastructure Diagnostics Agent",
        "Inspects Linux and Windows hosts with read-only tools: CPU, memory, disk, "
        "processes, services and system logs.",
        4,
        capabilities=("hosts", "kubernetes", "virtualisation"),
    ),
    AgentSpec(
        "application_diagnostics",
        "Application Diagnostics Agent",
        "Inspects containers and HTTP services: state, restarts, logs, HTTP health and "
        "recent changes.",
        4,
        capabilities=("logs", "applications"),
    ),
    AgentSpec(
        "network_diagnostics",
        "Network Diagnostics Agent",
        "Checks DNS resolution, TCP ports and HTTP reachability of inventoried resources "
        "from LocalOps.",
        4,
        capabilities=("dns", "connectivity", "tls"),
    ),
    AgentSpec(
        "root_cause_analysis",
        "Root Cause Analysis Agent",
        "Correlates evidence from the other agents into a root-cause analysis; runs "
        "automatically after the specialists.",
        4,
        capabilities=("correlation", "hypotheses"),
    ),
    AgentSpec(
        "change_impact",
        "Change Impact Agent",
        "Evaluates dependents, active incidents and environment to rate the risk of a "
        "proposed change and recommend precautions.",
        4,
        capabilities=("dependencies", "risk"),
    ),
    AgentSpec(
        "remediation_planner",
        "Remediation Planner Agent",
        "After the root-cause analysis, proposes corrective actions using registered "
        "remediation tools; policy and approvers decide whether they run.",
        5,
        capabilities=("remediation", "rollback"),
    ),
    AgentSpec(
        "verification",
        "Verification Agent",
        "Re-checks the affected resource after a remediation and decides, by fixed rules, "
        "whether it worked.",
        5,
        capabilities=("verification",),
    ),
    AgentSpec(
        "knowledge",
        "Knowledge Agent",
        "Searches past incidents and investigations for similar issues and their "
        "resolutions (runbooks arrive with the knowledge base).",
        4,
        capabilities=("rag", "runbooks"),
    ),
)

BY_TYPE: Final[dict[str, AgentSpec]] = {a.agent_type: a for a in CATALOGUE}
SPECIALIST_TYPES: Final[tuple[str, ...]] = tuple(a.agent_type for a in CATALOGUE if a.specialist)


def get_spec(agent_type: str) -> AgentSpec | None:
    return BY_TYPE.get(agent_type)


def merged_config(configuration: dict[str, Any] | None) -> dict[str, Any]:
    return {**DEFAULT_AGENT_CONFIG, **(configuration or {})}
