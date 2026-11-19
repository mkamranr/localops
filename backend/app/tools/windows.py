"""Windows diagnostics over WinRM. Scripts are constants; validated values are inserted
as single-quoted PowerShell literals (which never expand variables or subexpressions)."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import Field, field_validator

from app.tools.base import ResourceArgs, ToolError, ToolOutput, ToolSpec
from app.tools.context import ToolContext

WINDOWS_TYPES = ("windows_server",)
SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.$()-]{0,79}$")
FILTER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.:()-]{0,79}$")


def ps_literal(value: str) -> str:
    """Single-quoted PowerShell string literal."""
    return "'" + value.replace("'", "''") + "'"


def _json(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ToolError("PowerShell did not return JSON") from exc


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


async def _ps(ctx: ToolContext, script: str) -> Any:
    transport = await ctx.transport()
    return _json(await transport.powershell(script))


SNAPSHOT_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$os = Get-CimInstance Win32_OperatingSystem
$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
[pscustomobject]@{
  cpu_percent = $cpu
  cpu_count = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
  memory_total_mb = [math]::Round($os.TotalVisibleMemorySize / 1024)
  memory_free_mb = [math]::Round($os.FreePhysicalMemory / 1024)
  uptime_hours = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1)
  disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
    [pscustomobject]@{ drive = $_.DeviceID; size_gb = [math]::Round($_.Size / 1GB, 1);
      free_gb = [math]::Round($_.FreeSpace / 1GB, 1) } })
} | ConvertTo-Json -Depth 4 -Compress
"""


async def system_snapshot(ctx: ToolContext, args: ResourceArgs) -> ToolOutput:
    data = await _ps(ctx, SNAPSHOT_PS) or {}
    total, free = data.get("memory_total_mb") or 0, data.get("memory_free_mb") or 0
    mem = round((total - free) / total * 100, 1) if total else None
    summary = f"CPU {data.get('cpu_percent')}% on {data.get('cpu_count')} CPUs, memory {mem}% used"
    return ToolOutput(json.dumps(data, indent=1), summary, {"snapshot": data})


class TopProcessesArgs(ResourceArgs):
    sort_by: Literal["cpu", "memory"] = "cpu"
    limit: int = Field(15, ge=3, le=25)


async def top_processes(ctx: ToolContext, args: TopProcessesArgs) -> ToolOutput:
    prop = "PercentProcessorTime" if args.sort_by == "cpu" else "WorkingSetPrivate"
    script = (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        "$n = [int](Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors\n"
        "Get-CimInstance Win32_PerfFormattedData_PerfProc_Process | "
        "Where-Object { $_.Name -notin '_Total','Idle' } | "
        f"Sort-Object {prop} -Descending | Select-Object -First {args.limit} | "
        "ForEach-Object { [pscustomobject]@{ name = $_.Name; pid = $_.IDProcess; "
        "cpu_percent = [math]::Round($_.PercentProcessorTime / [math]::Max($n,1), 1); "
        "memory_mb = [math]::Round($_.WorkingSetPrivate / 1MB, 1) } } | "
        "ConvertTo-Json -Compress"
    )
    rows = _list(await _ps(ctx, script))
    top = rows[0] if rows else {}
    summary = (
        f"Top process by {args.sort_by}: {top.get('name')} "
        f"({top.get('cpu_percent')}% CPU, {top.get('memory_mb')} MB)"
        if top
        else "No process data"
    )
    return ToolOutput(json.dumps(rows, indent=1), summary, {"processes": rows})


class ServiceArgs(ResourceArgs):
    name: str = Field(description="Service name or display name, e.g. Spooler", max_length=80)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not SERVICE_RE.fullmatch(v):
            raise ValueError("name must be a Windows service name (letters, digits, space . _ $ -)")
        return v


async def service_status(ctx: ToolContext, args: ServiceArgs) -> ToolOutput:
    name = ps_literal(args.name)
    script = (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        f"$n = {name}\n"
        "$s = Get-CimInstance Win32_Service | Where-Object { $_.Name -eq $n -or "
        "$_.DisplayName -eq $n } | Select-Object -First 1\n"
        "if ($null -eq $s) { '{}' } else {\n"
        "$svc = Get-Service -Name $s.Name\n"
        "[pscustomobject]@{ name = $s.Name; display_name = $s.DisplayName; state = $s.State;"
        " start_mode = $s.StartMode; exit_code = $s.ExitCode;"
        " service_specific_exit_code = $s.ServiceSpecificExitCode; process_id = $s.ProcessId;"
        " account = $s.StartName;"
        ' depends_on = @($svc.ServicesDependedOn | ForEach-Object { "$($_.Name)=$($_.Status)" });'
        ' dependents = @($svc.DependentServices | ForEach-Object { "$($_.Name)=$($_.Status)" })'
        " } | ConvertTo-Json -Compress }"
    )
    data = await _ps(ctx, script) or {}
    if not data:
        return ToolOutput("{}", f"Service {args.name} was not found on this host")
    summary = (
        f"{data.get('display_name') or data.get('name')}: {data.get('state')} "
        f"(start mode {data.get('start_mode')}, exit code {data.get('exit_code')})"
    )
    return ToolOutput(json.dumps(data, indent=1), summary, {"service": data})


LEVELS = {"critical": "1", "error": "1,2", "warning": "1,2,3", "information": "1,2,3,4"}


class EventLogArgs(ResourceArgs):
    log: Literal["System", "Application"] = "System"
    level: Literal["critical", "error", "warning", "information"] = Field(
        "error", description="Minimum level"
    )
    since_minutes: int = Field(240, ge=5, le=10080)
    contains: str | None = Field(
        None, description="Only events whose message contains this text (e.g. a service name)"
    )
    max_events: int = Field(40, ge=5, le=100)

    @field_validator("contains")
    @classmethod
    def _contains(cls, v: str | None) -> str | None:
        if v and not FILTER_RE.fullmatch(v):
            raise ValueError("contains may only hold letters, digits, space and . _ : ( ) -")
        return v or None


async def event_log(ctx: ToolContext, args: EventLogArgs) -> ToolOutput:
    where = ""
    if args.contains:
        where = f"| Where-Object {{ $_.Message -like ('*' + {ps_literal(args.contains)} + '*') }} "
    script = (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        f"$f = @{{ LogName = {ps_literal(args.log)}; Level = {LEVELS[args.level]}; "
        f"StartTime = (Get-Date).AddMinutes(-{args.since_minutes}) }}\n"
        f"@(Get-WinEvent -FilterHashtable $f -MaxEvents 500 {where}"
        f"| Select-Object -First {args.max_events} | ForEach-Object {{ [pscustomobject]@{{ "
        "time = $_.TimeCreated.ToString('o'); id = $_.Id; level = $_.LevelDisplayName; "
        "provider = $_.ProviderName; message = ($_.Message -replace '\\s+', ' ')."
        "Substring(0, [math]::Min(400, ($_.Message -replace '\\s+', ' ').Length)) } }) | "
        "ConvertTo-Json -Compress"
    )
    events = _list(await _ps(ctx, script))
    ids = sorted({str(e.get("id")) for e in events})[:8]
    summary = (
        f"{len(events)} {args.log} events at level {args.level} or worse in the last "
        f"{args.since_minutes} min" + (f" (ids {', '.join(ids)})" if ids else "")
    )
    return ToolOutput(json.dumps(events, indent=1), summary, {"events": len(events)})


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "windows_system_snapshot",
        "CPU load, memory, uptime and fixed-disk free space of a Windows server.",
        ResourceArgs,
        system_snapshot,
        resource_types=WINDOWS_TYPES,
        category="windows",
    ),
    ToolSpec(
        "windows_top_processes",
        "Processes using the most CPU or memory right now on a Windows server.",
        TopProcessesArgs,
        top_processes,
        resource_types=WINDOWS_TYPES,
        category="windows",
    ),
    ToolSpec(
        "windows_service_status",
        "State, start mode, exit codes, account and dependencies of one Windows service.",
        ServiceArgs,
        service_status,
        resource_types=WINDOWS_TYPES,
        category="windows",
    ),
    ToolSpec(
        "windows_event_log",
        "Recent System or Application event log entries at or above a level, optionally "
        "only those mentioning some text (e.g. a service name).",
        EventLogArgs,
        event_log,
        resource_types=WINDOWS_TYPES,
        category="windows",
    ),
)
