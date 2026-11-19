"""Health evaluation from collected facts. Deterministic and explainable: every
non-healthy status carries human-readable reasons."""

from __future__ import annotations

from typing import Any

DISK_DEGRADED = 90.0
DISK_UNHEALTHY = 97.0
MEMORY_DEGRADED = 95.0
LOAD_PER_CPU_DEGRADED = 2.0
CERT_WARN_DAYS = 14
RESTARTS_DEGRADED = 5

_RANK = {"healthy": 0, "unknown": 1, "degraded": 2, "unhealthy": 3}


def worst(*statuses: str) -> str:
    return max(statuses, key=lambda s: _RANK.get(s, 1)) if statuses else "unknown"


def evaluate(resource_type: str, facts: dict[str, Any]) -> tuple[str, list[str]]:
    if resource_type in ("linux_server", "windows_server", "docker_host"):
        return _host(facts)
    if resource_type == "docker_container":
        return _container(facts.get("container") or {})
    if resource_type == "http_service":
        return _http(facts.get("http") or {})
    return "unknown", []


def _host(facts: dict[str, Any]) -> tuple[str, list[str]]:
    status, reasons = "healthy", []
    for disk in facts.get("disks") or []:
        pct = disk.get("used_percent")
        if pct is None:
            continue
        if pct >= DISK_UNHEALTHY:
            status = worst(status, "unhealthy")
            reasons.append(f"Disk {disk.get('mount')} is {pct:.0f}% full")
        elif pct >= DISK_DEGRADED:
            status = worst(status, "degraded")
            reasons.append(f"Disk {disk.get('mount')} is {pct:.0f}% full")
    mem = (facts.get("memory") or {}).get("used_percent")
    if mem is not None and mem >= MEMORY_DEGRADED:
        status = worst(status, "degraded")
        reasons.append(f"Memory is {mem:.0f}% used")
    load, cpus = facts.get("load") or [], facts.get("cpu_count") or 0
    if len(load) >= 2 and cpus and load[1] / cpus >= LOAD_PER_CPU_DEGRADED:
        status = worst(status, "degraded")
        reasons.append(f"5-minute load {load[1]:.1f} on {cpus} CPUs")
    failed = facts.get("failed_services") or []
    if failed:
        status = worst(status, "degraded")
        shown = ", ".join(failed[:5]) + (f" (+{len(failed) - 5} more)" if len(failed) > 5 else "")
        reasons.append(f"Failed services: {shown}")
    docker = facts.get("docker") or {}
    if docker.get("installed") and docker.get("accessible") is False:
        reasons.append(f"Docker is installed but not accessible ({docker.get('status')})")
    return status, reasons


def _container(c: dict[str, Any]) -> tuple[str, list[str]]:
    state, health = c.get("state"), c.get("health")
    restarts = c.get("restart_count") or 0
    if state == "running":
        if health == "unhealthy":
            return "unhealthy", ["Container health check is failing"]
        status, reasons = (
            ("degraded", ["Health check is starting"])
            if health == "starting"
            else (
                "healthy",
                [],
            )
        )
        if restarts >= RESTARTS_DEGRADED:
            status = worst(status, "degraded")
            reasons.append(f"Restarted {restarts} times")
        return status, reasons
    if state == "restarting":
        return "unhealthy", [f"Container is restarting (restarted {restarts} times)"]
    if state == "paused":
        return "degraded", ["Container is paused"]
    if state in ("exited", "dead"):
        code = c.get("exit_code")
        if state == "exited" and code == 0:
            return "unknown", ["Container is stopped (exit code 0)"]
        return "unhealthy", [f"Container {state} with exit code {code}"]
    return "unknown", [f"Container state: {state}"] if state else []


def _http(h: dict[str, Any]) -> tuple[str, list[str]]:
    if h.get("error"):
        return "unhealthy", [f"Request failed: {h['error']}"]
    status, reasons = "healthy", []
    if h.get("ok") is False:
        status = "unhealthy"
        reasons.append(f"Unexpected HTTP status {h.get('status_code')}")
    latency, slow = h.get("latency_ms"), h.get("slow_ms") or 2000
    if latency is not None and latency > slow:
        status = worst(status, "degraded")
        reasons.append(f"Slow response: {latency:.0f} ms")
    tls = h.get("tls") or {}
    days = tls.get("days_left")
    if days is not None:
        if days < 0:
            status = "unhealthy"
            reasons.append("TLS certificate has expired")
        elif days < CERT_WARN_DAYS:
            status = worst(status, "degraded")
            reasons.append(f"TLS certificate expires in {days} days")
    return status, reasons
