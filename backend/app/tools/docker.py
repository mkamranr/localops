"""Docker container diagnostics: over SSH (docker CLI) or the TLS Engine API."""

from __future__ import annotations

import json
import re
import shlex
import struct
import time
from typing import Any

from pydantic import Field

from app.tools.base import ResourceArgs, ToolError, ToolOutput, ToolSpec
from app.tools.context import ToolContext

CONTAINER_TYPES = ("docker_container",)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


async def _name(ctx: ToolContext) -> str:
    name = await ctx.container()
    if not NAME_RE.fullmatch(name):
        raise ToolError("The container name is not valid", code="invalid_arguments")
    return name


def _state_summary(raw: dict[str, Any]) -> dict[str, Any]:
    state = raw.get("State") or {}
    health = state.get("Health") or {}
    host_config = raw.get("HostConfig") or {}
    config = raw.get("Config") or {}
    return {
        "name": str(raw.get("Name", "")).lstrip("/"),
        "image": config.get("Image"),
        "status": state.get("Status"),
        "running": state.get("Running"),
        "restarting": state.get("Restarting"),
        "oom_killed": state.get("OOMKilled"),
        "exit_code": state.get("ExitCode"),
        "error": state.get("Error") or None,
        "started_at": state.get("StartedAt"),
        "finished_at": state.get("FinishedAt"),
        "restart_count": raw.get("RestartCount"),
        "restart_policy": (host_config.get("RestartPolicy") or {}).get("Name"),
        "memory_limit_bytes": host_config.get("Memory") or None,
        "health_status": health.get("Status"),
        "health_failing_streak": health.get("FailingStreak"),
        "health_log": [
            {
                "end": h.get("End"),
                "exit_code": h.get("ExitCode"),
                "output": str(h.get("Output", ""))[:300],
            }
            for h in (health.get("Log") or [])[-3:]
        ],
        # Environment *names* only: values often hold secrets.
        "env_names": sorted({str(e).split("=", 1)[0] for e in config.get("Env") or []}),
        "command": " ".join(
            str(p) for p in (config.get("Entrypoint") or []) + (config.get("Cmd") or [])
        )[:300],
    }


async def container_state(ctx: ToolContext, args: ResourceArgs) -> ToolOutput:
    name = await _name(ctx)
    conn = await ctx.connection()
    transport = await ctx.transport()
    if conn.kind == "docker_tls":
        resp = await transport.docker_get(f"/containers/{name}/json")
        if resp.status_code == 404:
            raise ToolError(f"Container {name} no longer exists", code="not_found")
        raw = resp.json()
    else:
        result = await transport.shell(f"docker inspect --type container -- {shlex.quote(name)}")
        if result.exit_status != 0:
            err = result.stderr.strip() or result.stdout.strip()
            if "permission denied" in err.lower():
                raise ToolError(
                    "The SSH user may not use Docker on this host", code="permission_denied"
                )
            if "no such" in err.lower():
                raise ToolError(f"Container {name} no longer exists", code="not_found")
            raise ToolError(f"docker inspect failed: {err[:200]}")
        docs = json.loads(result.stdout or "[]")
        raw = docs[0] if isinstance(docs, list) and docs else {}
    s = _state_summary(raw)
    summary = (
        f"{s['name']}: {s['status']}, exit code {s['exit_code']}, "
        f"{s['restart_count']} restarts"
        + (", OOM-killed" if s["oom_killed"] else "")
        + (f", health {s['health_status']}" if s["health_status"] else "")
    )
    return ToolOutput(json.dumps(s, indent=1, default=str), summary, {"state": s})


class LogsArgs(ResourceArgs):
    tail: int = Field(100, ge=10, le=300, description="Number of most recent lines")
    since_minutes: int | None = Field(None, ge=1, le=10080)


def demux(payload: bytes) -> str:
    """Decode the Engine API multiplexed log stream (8-byte frame headers)."""
    out: list[str] = []
    i = 0
    while i + 8 <= len(payload) and payload[i] in (0, 1, 2) and payload[i + 1 : i + 4] == b"\0\0\0":
        (size,) = struct.unpack(">I", payload[i + 4 : i + 8])
        out.append(payload[i + 8 : i + 8 + size].decode("utf-8", "replace"))
        i += 8 + size
    if i == 0:  # TTY containers are not multiplexed
        return payload.decode("utf-8", "replace")
    return "".join(out)


async def container_logs(ctx: ToolContext, args: LogsArgs) -> ToolOutput:
    name = await _name(ctx)
    conn = await ctx.connection()
    transport = await ctx.transport()
    if conn.kind == "docker_tls":
        query = f"stdout=1&stderr=1&timestamps=1&tail={args.tail}"
        if args.since_minutes:
            query += f"&since={int(time.time()) - args.since_minutes * 60}"
        resp = await transport.docker_get(f"/containers/{name}/logs?{query}")
        if resp.status_code >= 400:
            raise ToolError(f"Docker API returned HTTP {resp.status_code} for the logs")
        text = demux(resp.content)
    else:
        since = f" --since {args.since_minutes}m" if args.since_minutes else ""
        result = await transport.shell(
            f"docker logs --timestamps --tail {args.tail}{since} -- {shlex.quote(name)} 2>&1"
        )
        if result.exit_status != 0 and "permission denied" in result.stdout.lower():
            raise ToolError(
                "The SSH user may not use Docker on this host", code="permission_denied"
            )
        text = result.stdout
    lines = [line for line in text.splitlines() if line.strip()]
    errors = sum(
        1 for line in lines if re.search(r"(?i)\b(error|fatal|panic|exception|failed)\b", line)
    )
    last = lines[-1][:160] if lines else ""
    summary = f"{len(lines)} log lines, {errors} with error keywords" + (
        f"; last: {last}" if last else ""
    )
    return ToolOutput(text or "(no output)", summary, {"lines": len(lines), "errors": errors})


async def container_stats(ctx: ToolContext, args: ResourceArgs) -> ToolOutput:
    name = await _name(ctx)
    conn = await ctx.connection()
    transport = await ctx.transport()
    if conn.kind == "docker_tls":
        resp = await transport.docker_get(f"/containers/{name}/stats?stream=false")
        if resp.status_code >= 400:
            raise ToolError(f"Docker API returned HTTP {resp.status_code} for stats")
        raw = resp.json()
        cpu, pre = raw.get("cpu_stats") or {}, raw.get("precpu_stats") or {}
        delta = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (
            pre.get("cpu_usage") or {}
        ).get("total_usage", 0)
        system = (cpu.get("system_cpu_usage") or 0) - (pre.get("system_cpu_usage") or 0)
        cpus = cpu.get("online_cpus") or 1
        mem = raw.get("memory_stats") or {}
        stats = {
            "cpu_percent": round(delta / system * cpus * 100, 1) if system > 0 else None,
            "memory_usage_mb": round((mem.get("usage") or 0) / 1048576, 1),
            "memory_limit_mb": round((mem.get("limit") or 0) / 1048576, 1),
            "pids": (raw.get("pids_stats") or {}).get("current"),
        }
        text = json.dumps(stats)
    else:
        result = await transport.shell(
            f"docker stats --no-stream --format '{{{{json .}}}}' -- {shlex.quote(name)}"
        )
        if result.exit_status != 0:
            raise ToolError(f"docker stats failed: {(result.stderr or result.stdout)[:200]}")
        text = result.stdout
        try:
            raw = json.loads(text.strip().splitlines()[0])
            stats = {
                "cpu_percent": raw.get("CPUPerc"),
                "memory": raw.get("MemUsage"),
                "memory_percent": raw.get("MemPerc"),
                "pids": raw.get("PIDs"),
            }
        except (ValueError, IndexError):
            stats = {}
    summary = f"CPU {stats.get('cpu_percent', '?')}, memory " + str(
        stats.get("memory") or stats.get("memory_usage_mb", "?")
    )
    return ToolOutput(text, summary, {"stats": stats})


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "docker_container_state",
        "Status, exit code, restart count, OOM kill flag, health-check results, restart "
        "policy and environment variable names of a container.",
        ResourceArgs,
        container_state,
        resource_types=CONTAINER_TYPES,
        category="docker",
    ),
    ToolSpec(
        "docker_container_logs",
        "The most recent log lines (stdout and stderr) of a container.",
        LogsArgs,
        container_logs,
        resource_types=CONTAINER_TYPES,
        category="docker",
    ),
    ToolSpec(
        "docker_container_stats",
        "Current CPU, memory and process count of a running container.",
        ResourceArgs,
        container_stats,
        resource_types=CONTAINER_TYPES,
        category="docker",
    ),
)
