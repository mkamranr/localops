"""The read-only tool catalogue."""

from __future__ import annotations

from typing import Final

from app.tools import docker, inventory, linux, network, windows
from app.tools.base import ToolSpec

ALL_TOOLS: Final[tuple[ToolSpec, ...]] = (
    *linux.TOOLS,
    *windows.TOOLS,
    *docker.TOOLS,
    *network.TOOLS,
    *inventory.TOOLS,
)
TOOLS: Final[dict[str, ToolSpec]] = {t.name: t for t in ALL_TOOLS}

# Default tool sets per agent (overridable with the agent's ``allowed_tools`` config).
AGENT_TOOLS: Final[dict[str, tuple[str, ...]]] = {
    "infrastructure_diagnostics": (
        "inventory_resource",
        "resource_metrics",
        "recent_changes",
        "linux_system_snapshot",
        "linux_top_processes",
        "linux_service_status",
        "linux_failed_units",
        "linux_journal",
        "windows_system_snapshot",
        "windows_top_processes",
        "windows_service_status",
        "windows_event_log",
        "docker_container_state",
        "docker_container_stats",
    ),
    "application_diagnostics": (
        "inventory_resource",
        "inventory_dependencies",
        "recent_changes",
        "docker_container_state",
        "docker_container_logs",
        "docker_container_stats",
        "net_http_check",
        "linux_service_status",
        "linux_journal",
        "windows_service_status",
        "windows_event_log",
    ),
    "network_diagnostics": (
        "inventory_resource",
        "inventory_dependencies",
        "net_dns_lookup",
        "net_tcp_check",
        "net_http_check",
    ),
    "knowledge": ("search_similar_incidents",),
}


def get_tool(name: str) -> ToolSpec | None:
    return TOOLS.get(name)


def tools_for_agent(agent_type: str, allowed: list[str] | None = None) -> list[ToolSpec]:
    names = list(allowed) if allowed else list(AGENT_TOOLS.get(agent_type, ()))
    return [TOOLS[n] for n in names if n in TOOLS]
