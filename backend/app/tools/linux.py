"""Linux diagnostics over SSH. Commands are constant templates; arguments are
validated by the models below and shell-quoted before use."""

from __future__ import annotations

import re
import shlex
from typing import Literal

from pydantic import Field, field_validator

from app.tools.base import ResourceArgs, ToolError, ToolOutput, ToolSpec
from app.tools.context import ToolContext

LINUX_TYPES = ("linux_server", "docker_host")
UNIT_RE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9@._:\\-]{0,99}$")
Priority = Literal["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]


def valid_unit(value: str) -> str:
    value = value.strip()
    if not UNIT_RE.fullmatch(value):
        raise ValueError(
            "unit must be a systemd unit name such as nginx or nginx.service "
            "(letters, digits, @ . _ : - only)"
        )
    return value


async def _run(ctx: ToolContext, command: str) -> tuple[int, str, str]:
    transport = await ctx.transport()
    result = await transport.shell(command)
    return result.exit_status, result.stdout, result.stderr


def _combine(out: str, err: str) -> str:
    err = err.strip()
    return f"{out.rstrip()}\n[stderr]\n{err}" if err else out


# ------------------------------------------------------------------ system snapshot

SNAPSHOT_SCRIPT = (
    "echo '## uptime'; uptime; "
    "echo '## cpus'; nproc 2>/dev/null; "
    "echo '## loadavg'; cat /proc/loadavg; "
    "echo '## vmstat (1s x3)'; vmstat 1 3 2>/dev/null || head -n 1 /proc/stat; "
    "echo '## memory (MB)'; free -m 2>/dev/null || head -n 5 /proc/meminfo; "
    "echo '## disk'; df -hP -x tmpfs -x devtmpfs -x overlay -x squashfs 2>/dev/null; "
    "echo '## pressure'; for f in cpu memory io; do "
    "[ -r /proc/pressure/$f ] && echo $f: $(head -n 1 /proc/pressure/$f); done; "
    "echo '## oom (kernel, last 24h)'; "
    "journalctl -k -q --no-pager --since '24 hours ago' 2>/dev/null "
    "| grep -iE 'out of memory|oom-kill|killed process' | tail -n 5"
)


async def system_snapshot(ctx: ToolContext, args: ResourceArgs) -> ToolOutput:
    _status, out, err = await _run(ctx, SNAPSHOT_SCRIPT)
    load = re.search(r"## loadavg\n([\d.]+) ([\d.]+) ([\d.]+)", out)
    cpus = re.search(r"## cpus\n(\d+)", out)
    summary = "System snapshot"
    if load:
        summary += f": load {load.group(1)}/{load.group(2)}/{load.group(3)}"
        if cpus:
            summary += f" on {cpus.group(1)} CPUs"
    return ToolOutput(_combine(out, err), summary)


# ------------------------------------------------------------------ processes


class TopProcessesArgs(ResourceArgs):
    sort_by: Literal["cpu", "memory"] = "cpu"
    limit: int = Field(15, ge=3, le=25)


async def top_processes(ctx: ToolContext, args: TopProcessesArgs) -> ToolOutput:
    key = "-%cpu" if args.sort_by == "cpu" else "-%mem"
    command = (
        f"ps -eo pid,ppid,user,%cpu,%mem,rss,etime,stat,args --sort={key} "
        f"| head -n {args.limit + 1} | cut -c1-220"
    )
    status, out, err = await _run(ctx, command)
    if status != 0 and not out.strip():
        raise ToolError(f"ps failed: {err.strip()[:200]}")
    lines = out.strip().splitlines()
    top = " ".join(lines[1].split()[8:])[:80] if len(lines) > 1 else "none"
    cpu = lines[1].split()[3] if len(lines) > 1 and len(lines[1].split()) > 3 else "?"
    mem = lines[1].split()[4] if len(lines) > 1 and len(lines[1].split()) > 4 else "?"
    return ToolOutput(out, f"Top process by {args.sort_by}: {top} ({cpu}% CPU, {mem}% memory)")


# ------------------------------------------------------------------ services


class ServiceArgs(ResourceArgs):
    unit: str = Field(description="systemd unit, e.g. nginx or nginx.service", max_length=100)

    @field_validator("unit")
    @classmethod
    def _unit(cls, v: str) -> str:
        return valid_unit(v)


async def service_status(ctx: ToolContext, args: ServiceArgs) -> ToolOutput:
    unit = shlex.quote(args.unit)
    command = (
        f"systemctl show --no-pager -p Id,Description,LoadState,ActiveState,SubState,Result,"
        f"ExecMainStatus,ExecMainCode,NRestarts,ActiveEnterTimestamp,InactiveEnterTimestamp "
        f"-- {unit}; echo '## status'; systemctl status --no-pager --lines=15 -- {unit}"
    )
    _status, out, err = await _run(ctx, command)
    props = dict(
        line.split("=", 1) for line in out.splitlines() if "=" in line and not line.startswith(" ")
    )
    if props.get("LoadState") == "not-found":
        return ToolOutput(_combine(out, err), f"Unit {args.unit} does not exist on this host")
    summary = (
        f"{props.get('Id', args.unit)}: {props.get('ActiveState', '?')}/"
        f"{props.get('SubState', '?')}, result {props.get('Result', '?')}, "
        f"restarts {props.get('NRestarts', '?')}"
    )
    return ToolOutput(_combine(out, err), summary)


async def failed_units(ctx: ToolContext, args: ResourceArgs) -> ToolOutput:
    _status, out, _err = await _run(
        ctx, "systemctl list-units --failed --no-pager --no-legend --plain 2>&1"
    )
    units = [line.split()[0] for line in out.splitlines() if line.strip()]
    summary = f"Failed units: {', '.join(units[:8])}" if units else "No failed systemd units"
    return ToolOutput(out or "(none)", summary)


# ------------------------------------------------------------------ journal


class JournalArgs(ResourceArgs):
    unit: str | None = Field(None, description="Limit to one systemd unit", max_length=100)
    priority: Priority = Field("warning", description="Minimum priority to include")
    since_minutes: int = Field(60, ge=1, le=1440)
    lines: int = Field(100, ge=10, le=300)

    @field_validator("unit")
    @classmethod
    def _unit(cls, v: str | None) -> str | None:
        return valid_unit(v) if v else None


async def journal(ctx: ToolContext, args: JournalArgs) -> ToolOutput:
    unit = f" -u {shlex.quote(args.unit)}" if args.unit else ""
    command = (
        f"if command -v journalctl >/dev/null 2>&1; then "
        f"journalctl --no-pager -q -o short-iso --since '{args.since_minutes} min ago' "
        f"-p {args.priority} -n {args.lines}{unit} 2>&1; "
        f"else tail -n {args.lines} /var/log/syslog /var/log/messages 2>&1; fi"
    )
    _status, out, _err = await _run(ctx, command)
    if "insufficient permissions" in out or "No journal files were opened" in out:
        raise ToolError(
            "The SSH user cannot read the systemd journal. Add it to the systemd-journal "
            "(or adm) group on the host.",
            code="permission_denied",
        )
    entries = [line for line in out.splitlines() if line.strip() and not line.startswith("--")]
    scope = f" for {args.unit}" if args.unit else ""
    summary = (
        f"{len(entries)} journal entries{scope} at priority {args.priority} or higher "
        f"in the last {args.since_minutes} min"
    )
    return ToolOutput(out or "(no entries)", summary, {"entries": len(entries)})


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "linux_system_snapshot",
        "CPU load, CPU count, vmstat, memory, disk usage, pressure stall info and recent "
        "OOM-killer messages of a Linux host.",
        ResourceArgs,
        system_snapshot,
        resource_types=LINUX_TYPES,
        category="linux",
    ),
    ToolSpec(
        "linux_top_processes",
        "Processes using the most CPU or memory on a Linux host (pid, user, %cpu, %mem, "
        "elapsed time, command line).",
        TopProcessesArgs,
        top_processes,
        resource_types=LINUX_TYPES,
        category="linux",
    ),
    ToolSpec(
        "linux_service_status",
        "State, result, restart count and recent log lines of one systemd service.",
        ServiceArgs,
        service_status,
        resource_types=LINUX_TYPES,
        category="linux",
    ),
    ToolSpec(
        "linux_failed_units",
        "List systemd units in the failed state.",
        ResourceArgs,
        failed_units,
        resource_types=LINUX_TYPES,
        category="linux",
    ),
    ToolSpec(
        "linux_journal",
        "Recent systemd journal entries, optionally for one unit, filtered by priority.",
        JournalArgs,
        journal,
        resource_types=LINUX_TYPES,
        category="linux",
    ),
)
