"""Diagnostics specialists built on the shared tool loop."""

from __future__ import annotations

from typing import Final

ROLES: Final[dict[str, str]] = {
    "infrastructure_diagnostics": (
        "You inspect hosts: CPU, memory, disk, processes, services and system logs "
        "(the systemd journal or the Windows event log)."
    ),
    "application_diagnostics": (
        "You inspect applications: container state, restarts and logs, HTTP health, "
        "application services and recent changes."
    ),
    "network_diagnostics": (
        "You test reachability from LocalOps: DNS resolution, TCP ports and HTTP endpoints "
        "of inventoried resources and their dependencies. Results describe the path from "
        "LocalOps, not necessarily between the resources themselves."
    ),
}

TOOL_LOOP_AGENTS: Final[frozenset[str]] = frozenset(ROLES)

# Which diagnostics agent covers a resource type when the plan forgot one.
COVERAGE: Final[dict[str, str]] = {
    "linux_server": "infrastructure_diagnostics",
    "windows_server": "infrastructure_diagnostics",
    "docker_host": "infrastructure_diagnostics",
    "docker_container": "application_diagnostics",
    "http_service": "application_diagnostics",
    "application": "application_diagnostics",
}
