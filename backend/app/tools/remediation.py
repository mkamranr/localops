"""State-changing remediation tools (phase 5).

These run *only* through the execution gateway (``app.remediation.gateway``), after
validation, permission and policy checks and, where required, an approval bound to
the exact arguments. Commands are constant templates; every argument is validated by
the models below and then shell-quoted or inserted as a PowerShell literal.

Each tool also has a ``describe_action`` that states, in plain words, exactly which
operation will run; that text is what an approver sees.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any, Literal

from pydantic import Field, field_validator

from app.tools.base import ResourceArgs, ResourceInfo, ToolError, ToolOutput, ToolSpec
from app.tools.context import ToolContext
from app.tools.docker import CONTAINER_TYPES, NAME_RE
from app.tools.linux import LINUX_TYPES, valid_unit
from app.tools.windows import SERVICE_RE, WINDOWS_TYPES, ps_literal

SUDO_HINT = (
    "The SSH user may not run this with sudo without a password. Add a sudoers rule for "
    "it (see the installation guide, 'Remediation prerequisites')."
)
# Text that must match the target process's command line before it is terminated.
EXPECTED_RE = re.compile(r"^[^\x00-\x1f\x7f]{3,200}$")
MARK = "LOCALOPS_"


def _sudo_denied(text: str) -> bool:
    lowered = text.lower()
    return (
        "a password is required" in lowered
        or "a terminal is required" in lowered
        or "is not allowed to" in lowered
        or "not in the sudoers" in lowered
        or "may not run sudo" in lowered
    )


async def _shell(ctx: ToolContext, command: str) -> tuple[int, str, str]:
    transport = await ctx.transport()
    result = await transport.shell(command)
    return result.exit_status, result.stdout, result.stderr


def _text(out: str, err: str) -> str:
    err = err.strip()
    return f"{out.rstrip()}\n[stderr]\n{err}" if err else out


# ------------------------------------------------------------------ Linux services

LinuxServiceOp = Literal["restart", "start", "stop"]


class LinuxServiceArgs(ResourceArgs):
    unit: str = Field(description="systemd unit, e.g. nginx or nginx.service", max_length=100)

    @field_validator("unit")
    @classmethod
    def _unit(cls, v: str) -> str:
        return valid_unit(v)


def _linux_service_command(op: str, unit: str) -> str:
    q = shlex.quote(unit)
    return (
        f"sudo -n systemctl {op} -- {q} 2>&1; rc=$?; "
        f"echo '{MARK}RC='$rc; echo '{MARK}STATE='$(systemctl is-active -- {q} 2>/dev/null)"
    )


def _linux_service_runner(op: str) -> Any:
    async def run(ctx: ToolContext, args: LinuxServiceArgs) -> ToolOutput:
        _status, out, err = await _shell(ctx, _linux_service_command(op, args.unit))
        rc = re.search(rf"{MARK}RC=(\d+)", out)
        state = re.search(rf"{MARK}STATE=(\S*)", out)
        body = re.sub(rf"^{MARK}.*$\n?", "", out, flags=re.M).strip()
        if _sudo_denied(body + err):
            raise ToolError(SUDO_HINT, code="permission_denied")
        code = int(rc.group(1)) if rc else 1
        active = state.group(1) if state else "unknown"
        if code != 0:
            raise ToolError(
                f"systemctl {op} {args.unit} failed (exit {code}): {(body or err).strip()[:300]}",
                code="command_failed",
            )
        return ToolOutput(
            _text(body or f"systemctl {op} {args.unit}: ok", err),
            f"systemctl {op} {args.unit} succeeded; unit is now {active}",
            {"unit": args.unit, "operation": op, "active_state": active},
        )

    return run


def _describe_linux_service(op: str) -> Any:
    def describe(args: LinuxServiceArgs, resource: ResourceInfo) -> str:
        return (
            f"{op.capitalize()} the systemd unit '{args.unit}' on {resource.name} "
            f"(runs: sudo -n systemctl {op} -- {shlex.quote(args.unit)})"
        )

    return describe


# ------------------------------------------------------------------ Docker containers

ContainerOp = Literal["restart", "start", "stop"]


class ContainerArgs(ResourceArgs):
    timeout_seconds: int = Field(
        10, ge=0, le=120, description="Seconds to wait for a clean stop before killing"
    )


def _container_runner(op: str) -> Any:
    async def run(ctx: ToolContext, args: ContainerArgs) -> ToolOutput:
        name = await ctx.container()
        if not NAME_RE.fullmatch(name):
            raise ToolError("The container name is not valid", code="invalid_arguments")
        conn = await ctx.connection()
        transport = await ctx.transport()
        if conn.kind == "docker_tls":
            query = f"?t={args.timeout_seconds}" if op != "start" else ""
            resp = await transport.docker_post(f"/containers/{name}/{op}{query}")
            if resp.status_code == 404:
                raise ToolError(f"Container {name} no longer exists", code="not_found")
            if resp.status_code not in (204, 304):
                raise ToolError(
                    f"Docker API {op} returned HTTP {resp.status_code}: {resp.text[:200]}",
                    code="command_failed",
                )
            note = "already in that state" if resp.status_code == 304 else "ok"
            return ToolOutput(
                f"POST /containers/{name}/{op}: {note}",
                f"docker {op} {name}: {note}",
                {"container": name, "operation": op},
            )
        flag = f" -t {args.timeout_seconds}" if op != "start" else ""
        status, out, err = await _shell(ctx, f"docker {op}{flag} -- {shlex.quote(name)}")
        if status != 0:
            detail = (err or out).strip()
            if "permission denied" in detail.lower():
                raise ToolError(
                    "The SSH user may not use Docker on this host", code="permission_denied"
                )
            raise ToolError(f"docker {op} {name} failed: {detail[:300]}", code="command_failed")
        return ToolOutput(
            _text(out or f"docker {op} {name}: ok", err),
            f"docker {op} {name} succeeded",
            {"container": name, "operation": op},
        )

    return run


def _describe_container(op: str) -> Any:
    def describe(args: ContainerArgs, resource: ResourceInfo) -> str:
        wait = f", waiting up to {args.timeout_seconds}s for a clean stop" if op != "start" else ""
        return f"{op.capitalize()} the Docker container '{resource.name}'{wait} (docker {op})"

    return describe


# ------------------------------------------------------------------ Windows services


class WindowsServiceArgs(ResourceArgs):
    name: str = Field(description="Service name, e.g. Spooler", max_length=80)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not SERVICE_RE.fullmatch(v):
            raise ValueError("name must be a Windows service name (letters, digits, space . _ $ -)")
        return v


WIN_CMDLET = {"restart": "Restart-Service", "start": "Start-Service", "stop": "Stop-Service"}


def _windows_service_script(op: str, name: str) -> str:
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"$n = {ps_literal(name)}\n"
        "$svc = Get-Service | Where-Object { $_.Name -eq $n -or $_.DisplayName -eq $n } "
        "| Select-Object -First 1\n"
        "if ($null -eq $svc) { [pscustomobject]@{ found = $false } | ConvertTo-Json -Compress; "
        "return }\n"
        f"{WIN_CMDLET[op]} -Name $svc.Name -Force\n"
        "$svc.Refresh()\n"
        "[pscustomobject]@{ found = $true; name = $svc.Name; display_name = $svc.DisplayName; "
        "status = [string]$svc.Status } | ConvertTo-Json -Compress"
    )


def _windows_service_runner(op: str) -> Any:
    async def run(ctx: ToolContext, args: WindowsServiceArgs) -> ToolOutput:
        transport = await ctx.transport()
        try:
            raw = await transport.powershell(_windows_service_script(op, args.name))
        except ToolError as exc:
            if "access is denied" in str(exc).lower() or "cannot open" in str(exc).lower():
                raise ToolError(
                    "The WinRM account may not control this service. Grant it service "
                    "control rights or use an administrative account.",
                    code="permission_denied",
                ) from exc
            raise
        data = json.loads(raw.strip() or "{}")
        if not data.get("found"):
            raise ToolError(f"Service {args.name} was not found on this host", code="not_found")
        return ToolOutput(
            json.dumps(data, indent=1),
            f"{WIN_CMDLET[op]} {data.get('name')} succeeded; status {data.get('status')}",
            {"service": data.get("name"), "operation": op, "status": data.get("status")},
        )

    return run


def _describe_windows_service(op: str) -> Any:
    def describe(args: WindowsServiceArgs, resource: ResourceInfo) -> str:
        return (
            f"{op.capitalize()} the Windows service '{args.name}' on {resource.name} "
            f"({WIN_CMDLET[op]} -Name {ps_literal(args.name)} -Force)"
        )

    return describe


# ------------------------------------------------------------------ processes


class ProcessArgs(ResourceArgs):
    pid: int = Field(ge=2, le=4_194_304, description="Process id (never 1)")
    expected_command: str = Field(
        description=(
            "Text that must appear in the process's command line, e.g. 'python worker.py'. "
            "The process is only terminated if it matches, so a reused PID is never killed."
        ),
        min_length=3,
        max_length=200,
    )
    force: bool = Field(False, description="Kill (SIGKILL / -Force) if it does not exit")

    @field_validator("expected_command")
    @classmethod
    def _expected(cls, v: str) -> str:
        if not EXPECTED_RE.fullmatch(v):
            raise ValueError("expected_command must be 3-200 printable characters")
        return v


class LinuxProcessArgs(ProcessArgs):
    use_sudo: bool = Field(
        False, description="Use sudo -n kill (needed for processes of other users)"
    )


def _linux_kill_script(args: LinuxProcessArgs) -> str:
    pid = int(args.pid)
    expected = shlex.quote(args.expected_command)
    kill = "sudo -n kill" if args.use_sudo else "kill"
    force = (
        f"if ps -p {pid} >/dev/null 2>&1; then {kill} -s KILL -- {pid} 2>&1; sleep 1; fi; "
        if args.force
        else ""
    )
    # One script, so the command-line check and the signal are as close as possible.
    return (
        f"cmd=$(ps -o args= -p {pid} 2>/dev/null); "
        f"if [ -z \"$cmd\" ]; then echo '{MARK}NOPROC'; exit 0; fi; "
        f"case \"$cmd\" in \\[*\\]) echo '{MARK}KERNEL'; exit 0;; esac; "
        f'case "$cmd" in *{expected}*) ;; *) echo \'{MARK}MISMATCH\'"$cmd"; exit 0;; esac; '
        f"{kill} -s TERM -- {pid} 2>&1; echo '{MARK}RC='$?; sleep 2; {force}"
        f"if ps -p {pid} >/dev/null 2>&1; then echo '{MARK}ALIVE'; else echo '{MARK}GONE'; fi"
    )


async def linux_terminate(ctx: ToolContext, args: LinuxProcessArgs) -> ToolOutput:
    _status, out, err = await _shell(ctx, _linux_kill_script(args))
    if f"{MARK}NOPROC" in out:
        raise ToolError(f"No process with PID {args.pid} is running", code="not_found")
    if f"{MARK}KERNEL" in out:
        raise ToolError(f"PID {args.pid} is a kernel thread", code="refused")
    mismatch = re.search(rf"{MARK}MISMATCH(.*)", out)
    if mismatch:
        raise ToolError(
            f"PID {args.pid} is now '{mismatch.group(1)[:120]}', which does not contain "
            f"'{args.expected_command}'. Nothing was terminated.",
            code="refused",
        )
    if _sudo_denied(out + err):
        raise ToolError(SUDO_HINT, code="permission_denied")
    if "operation not permitted" in (out + err).lower():
        raise ToolError(
            "The SSH user may not signal this process. Set use_sudo and add a sudoers rule.",
            code="permission_denied",
        )
    gone = f"{MARK}GONE" in out
    summary = (
        f"Process {args.pid} ({args.expected_command}) terminated"
        if gone
        else f"Sent SIGTERM to {args.pid} but it is still running"
    )
    body = re.sub(rf"^{MARK}.*$\n?", "", out, flags=re.M).strip()
    return ToolOutput(_text(body or summary, err), summary, {"pid": args.pid, "terminated": gone})


def _describe_linux_process(args: LinuxProcessArgs, resource: ResourceInfo) -> str:
    how = "SIGTERM, then SIGKILL if still running" if args.force else "SIGTERM"
    sudo = " with sudo" if args.use_sudo else ""
    return (
        f"Terminate process {args.pid} on {resource.name}{sudo} ({how}), only if its command "
        f"line contains '{args.expected_command}'"
    )


def _windows_kill_script(args: ProcessArgs) -> str:
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {int(args.pid)}'\n"
        "if ($null -eq $p) { [pscustomobject]@{ state = 'noproc' } | ConvertTo-Json -Compress; "
        "return }\n"
        "$cmd = [string]$p.CommandLine; if (-not $cmd) { $cmd = [string]$p.Name }\n"
        f"$e = {ps_literal(args.expected_command)}\n"
        "if (-not $cmd.ToLowerInvariant().Contains($e.ToLowerInvariant())) { "
        "[pscustomobject]@{ state = 'mismatch'; command = $cmd } | ConvertTo-Json -Compress; "
        "return }\n"
        f"Stop-Process -Id {int(args.pid)}{' -Force' if args.force else ''}\n"
        "Start-Sleep -Seconds 2\n"
        f"$alive = $null -ne (Get-Process -Id {int(args.pid)} -ErrorAction SilentlyContinue)\n"
        "[pscustomobject]@{ state = $(if ($alive) { 'alive' } else { 'gone' }); command = $cmd } "
        "| ConvertTo-Json -Compress"
    )


async def windows_terminate(ctx: ToolContext, args: ProcessArgs) -> ToolOutput:
    transport = await ctx.transport()
    data = json.loads((await transport.powershell(_windows_kill_script(args))).strip() or "{}")
    state = data.get("state")
    if state == "noproc":
        raise ToolError(f"No process with PID {args.pid} is running", code="not_found")
    if state == "mismatch":
        raise ToolError(
            f"PID {args.pid} is now '{str(data.get('command'))[:120]}', which does not contain "
            f"'{args.expected_command}'. Nothing was terminated.",
            code="refused",
        )
    gone = state == "gone"
    summary = (
        f"Process {args.pid} ({args.expected_command}) terminated"
        if gone
        else f"Stop-Process sent to {args.pid} but it is still running"
    )
    return ToolOutput(json.dumps(data, indent=1), summary, {"pid": args.pid, "terminated": gone})


def _describe_windows_process(args: ProcessArgs, resource: ResourceInfo) -> str:
    force = " -Force" if args.force else ""
    return (
        f"Terminate process {args.pid} on {resource.name} (Stop-Process -Id {args.pid}{force}), "
        f"only if its command line contains '{args.expected_command}'"
    )


# ------------------------------------------------------------------ reboot


class RebootArgs(ResourceArgs):
    delay_seconds: int = Field(5, ge=5, le=600, description="Seconds before the reboot starts")


async def linux_reboot(ctx: ToolContext, args: RebootArgs) -> ToolOutput:
    # Check the sudo rule first so a refusal is reported, then reboot in the background
    # so this session can return before the host goes down.
    command = (
        "sudo -n -l systemctl reboot >/dev/null 2>&1 || { echo 'LOCALOPS_DENIED'; exit 0; }; "
        f"nohup sh -c 'sleep {int(args.delay_seconds)}; sudo -n systemctl reboot' "
        ">/dev/null 2>&1 & echo 'LOCALOPS_SCHEDULED'"
    )
    _status, out, err = await _shell(ctx, command)
    if f"{MARK}DENIED" in out:
        raise ToolError(SUDO_HINT, code="permission_denied")
    if f"{MARK}SCHEDULED" not in out:
        raise ToolError(f"Could not schedule the reboot: {(err or out).strip()[:300]}")
    return ToolOutput(
        f"Reboot scheduled in {args.delay_seconds}s",
        f"Reboot of {ctx.require_resource().name} scheduled in {args.delay_seconds}s",
        {"rebooting": True},
    )


async def windows_reboot(ctx: ToolContext, args: RebootArgs) -> ToolOutput:
    transport = await ctx.transport()
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"shutdown.exe /r /t {int(args.delay_seconds)} /c 'LocalOps approved reboot' /d p:4:1\n"
        "'scheduled'"
    )
    out = await transport.powershell(script)
    if "scheduled" not in out:
        raise ToolError(f"Could not schedule the reboot: {out.strip()[:300]}")
    return ToolOutput(
        f"Reboot scheduled in {args.delay_seconds}s",
        f"Reboot of {ctx.require_resource().name} scheduled in {args.delay_seconds}s",
        {"rebooting": True},
    )


def _describe_reboot(args: RebootArgs, resource: ResourceInfo) -> str:
    return f"Reboot {resource.name} in {args.delay_seconds} seconds (all services restart)"


# ------------------------------------------------------------------ catalogue


def _spec(
    name: str,
    description: str,
    args: type[ResourceArgs],
    run: Any,
    describe: Any,
    *,
    types: tuple[str, ...],
    risk: str,
    verify: tuple[str, ...],
    category: str,
    timeout: float = 90.0,
) -> ToolSpec:
    return ToolSpec(
        name,
        description,
        args,
        run,
        resource_types=types,
        category=category,
        kind="change",
        default_risk=risk,
        describe_action=describe,
        verify_with=verify,
        timeout_seconds=timeout,
    )


TOOLS: tuple[ToolSpec, ...] = (
    *(
        _spec(
            f"linux_service_{op}",
            f"{op.capitalize()} a systemd service on a Linux host (sudo -n systemctl {op}).",
            LinuxServiceArgs,
            _linux_service_runner(op),
            _describe_linux_service(op),
            types=LINUX_TYPES,
            risk=risk,
            verify=("linux_service_status",),
            category="linux",
        )
        for op, risk in (("restart", "high"), ("start", "medium"), ("stop", "high"))
    ),
    *(
        _spec(
            f"docker_container_{op}",
            f"{op.capitalize()} a Docker container.",
            ContainerArgs,
            _container_runner(op),
            _describe_container(op),
            types=CONTAINER_TYPES,
            risk=risk,
            verify=("docker_container_state",),
            category="docker",
            timeout=150.0,
        )
        for op, risk in (("restart", "medium"), ("start", "medium"), ("stop", "high"))
    ),
    *(
        _spec(
            f"windows_service_{op}",
            f"{op.capitalize()} a Windows service ({WIN_CMDLET[op]}).",
            WindowsServiceArgs,
            _windows_service_runner(op),
            _describe_windows_service(op),
            types=WINDOWS_TYPES,
            risk=risk,
            verify=("windows_service_status",),
            category="windows",
        )
        for op, risk in (("restart", "high"), ("start", "medium"), ("stop", "high"))
    ),
    _spec(
        "linux_process_terminate",
        "Terminate one process on a Linux host by PID, only if its command line contains "
        "expected_command.",
        LinuxProcessArgs,
        linux_terminate,
        _describe_linux_process,
        types=LINUX_TYPES,
        risk="high",
        verify=("linux_top_processes",),
        category="linux",
    ),
    _spec(
        "windows_process_terminate",
        "Terminate one process on a Windows host by PID, only if its command line contains "
        "expected_command.",
        ProcessArgs,
        windows_terminate,
        _describe_windows_process,
        types=WINDOWS_TYPES,
        risk="high",
        verify=("windows_top_processes",),
        category="windows",
    ),
    _spec(
        "linux_reboot",
        "Reboot a Linux host.",
        RebootArgs,
        linux_reboot,
        _describe_reboot,
        types=LINUX_TYPES,
        risk="critical",
        verify=("linux_system_snapshot",),
        category="linux",
    ),
    _spec(
        "windows_reboot",
        "Reboot a Windows host.",
        RebootArgs,
        windows_reboot,
        _describe_reboot,
        types=WINDOWS_TYPES,
        risk="critical",
        verify=("windows_system_snapshot",),
        category="windows",
    ),
)

# What the verification step passes to each verify tool, from the change's arguments.
VERIFY_ARGS: dict[str, tuple[str, ...]] = {
    "linux_service_status": ("unit",),
    "windows_service_status": ("name",),
}
