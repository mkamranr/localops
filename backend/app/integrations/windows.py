"""Windows hosts over WinRM (pywinrm): one fixed, read-only PowerShell script -> JSON."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.integrations.base import (
    CollectorError,
    DiscoveredResource,
    Target,
    TargetResult,
    TestOutcome,
    clean_ips,
)

WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
$csp = Get-CimInstance Win32_ComputerSystemProduct
$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
$disks = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" |
  Select-Object DeviceID, FileSystem, Size, FreeSpace)
$svc = @(Get-CimInstance Win32_Service |
  Where-Object { $_.State -eq 'Running' -or $_.StartMode -eq 'Auto' } |
  Select-Object Name, DisplayName, State, StartMode)
$ips = @(Get-NetIPAddress -AddressFamily IPv4 | Select-Object -ExpandProperty IPAddress)
$listen = @(Get-NetTCPConnection -State Listen | Select-Object LocalAddress, LocalPort)
$est = @(Get-NetTCPConnection -State Established |
  Select-Object LocalPort, RemoteAddress, RemotePort)
[pscustomobject]@{
  hostname = $env:COMPUTERNAME
  domain = $cs.Domain
  uuid = $csp.UUID
  os = $os.Caption
  version = $os.Version
  build = $os.BuildNumber
  last_boot = $os.LastBootUpTime.ToUniversalTime().ToString('o')
  total_memory_kb = $os.TotalVisibleMemorySize
  free_memory_kb = $os.FreePhysicalMemory
  cpu_count = $cs.NumberOfLogicalProcessors
  cpu_percent = $cpu
  disks = $disks
  services = $svc
  ips = $ips
  listen = $listen
  established = $est
} | ConvertTo-Json -Depth 4 -Compress
"""

TEST_SCRIPT = "$env:COMPUTERNAME; (Get-CimInstance Win32_OperatingSystem).Caption"

MAX_ITEMS = 200

# (host, port, script) -> (exit status, stdout, stderr); replaced in tests.
RunPS = Callable[[str, int, str], tuple[int, str, str]]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse_windows(raw: dict[str, Any], target_host: str) -> DiscoveredResource:
    disks = []
    for d in _as_list(raw.get("disks")):
        size, free = d.get("Size") or 0, d.get("FreeSpace") or 0
        if not size:
            continue
        disks.append(
            {
                "mount": d.get("DeviceID"),
                "type": d.get("FileSystem"),
                "size_gb": round(size / 1024**3, 1),
                "used_gb": round((size - free) / 1024**3, 1),
                "used_percent": round((size - free) / size * 100, 1),
            }
        )
    services = _as_list(raw.get("services"))
    running = [
        {"name": s.get("Name"), "description": s.get("DisplayName") or ""}
        for s in services
        if s.get("State") == "Running"
    ][: MAX_ITEMS * 2]
    stopped_auto = [
        s.get("Name")
        for s in services
        if s.get("StartMode") == "Auto" and s.get("State") != "Running"
    ][:MAX_ITEMS]
    listen_ports: dict[int, dict[str, Any]] = {}
    for item in _as_list(raw.get("listen")):
        port = item.get("LocalPort")
        if isinstance(port, int):
            entry = listen_ports.setdefault(port, {"port": port, "addresses": []})
            addr = str(item.get("LocalAddress") or "")
            if addr and addr not in entry["addresses"]:
                entry["addresses"].append(addr)
    outbound: dict[tuple[str, int], int] = {}
    inbound: dict[tuple[str, int], int] = {}
    for c in _as_list(raw.get("established")):
        rip, rport, lport = (
            str(c.get("RemoteAddress") or ""),
            c.get("RemotePort"),
            c.get("LocalPort"),
        )
        if not clean_ips([rip]) or not isinstance(rport, int) or not isinstance(lport, int):
            continue
        if lport in listen_ports:
            inbound[(rip, lport)] = inbound.get((rip, lport), 0) + 1
        else:
            outbound[(rip, rport)] = outbound.get((rip, rport), 0) + 1
    total_kb, free_kb = raw.get("total_memory_kb") or 0, raw.get("free_memory_kb") or 0
    memory_percent = round((total_kb - free_kb) / total_kb * 100, 1) if total_kb else None
    uptime = None
    try:
        boot = datetime.fromisoformat(str(raw.get("last_boot")))
        uptime = int((datetime.now(UTC) - boot.astimezone(UTC)).total_seconds())
    except ValueError:
        pass
    hostname = str(raw.get("hostname") or target_host)
    domain = raw.get("domain")
    fqdn = f"{hostname}.{domain}".lower() if domain and "." in str(domain) else hostname
    machine_uuid = str(raw.get("uuid") or "").strip().lower()
    valid_uuid = len(machine_uuid) == 36 and machine_uuid != "ffffffff-ffff-ffff-ffff-ffffffffffff"
    cpu_percent = raw.get("cpu_percent")
    facts: dict[str, Any] = {
        "collector": "windows_winrm",
        "os": {"name": raw.get("os"), "version": raw.get("version"), "build": raw.get("build")},
        "domain": domain,
        "uptime_seconds": uptime,
        "cpu_count": raw.get("cpu_count"),
        "cpu_percent": cpu_percent,
        "memory": {
            "total_mb": round(total_kb / 1024) if total_kb else None,
            "available_mb": round(free_kb / 1024) if total_kb else None,
            "used_percent": memory_percent,
        },
        "disks": disks,
        "services": running,
        "stopped_auto_services": stopped_auto,
        "listening_ports": [listen_ports[p] for p in sorted(listen_ports)][:MAX_ITEMS],
        "connections": {
            "outbound": [
                {"remote_ip": h, "remote_port": p, "count": n}
                for (h, p), n in sorted(outbound.items())
            ][:MAX_ITEMS],
            "inbound": [
                {"remote_ip": h, "local_port": p, "count": n}
                for (h, p), n in sorted(inbound.items())
            ][:MAX_ITEMS],
        },
    }
    return DiscoveredResource(
        external_id=f"host:{machine_uuid}" if valid_uuid else f"host:{target_host.lower()}",
        name=hostname,
        resource_type="windows_server",
        hostname=fqdn,
        ip_addresses=clean_ips([*map(str, _as_list(raw.get("ips"))), target_host]),
        os=raw.get("os"),
        facts=facts,
        metrics={
            # Windows has no load average; use CPU utilisation as a fraction of 1 core-set.
            "cpu_load": round(float(cpu_percent) / 100 * (raw.get("cpu_count") or 1), 2)
            if isinstance(cpu_percent, (int, float))
            else None,
            "cpu_count": raw.get("cpu_count"),
            "memory_percent": memory_percent,
            "disk_percent": max((d["used_percent"] for d in disks), default=None),
        },
    )


def _pywinrm_runner(config: dict[str, Any], credentials: dict[str, Any]) -> RunPS:
    def run(host: str, port: int, script: str) -> tuple[int, str, str]:
        import winrm  # imported lazily: only needed when Windows integrations exist

        use_https = bool(config.get("use_https", port == 5986))
        session = winrm.Session(
            f"{'https' if use_https else 'http'}://{host}:{port}/wsman",
            auth=(credentials.get("username", ""), credentials.get("password", "")),
            transport=str(config.get("transport") or "ntlm"),
            server_cert_validation="validate" if config.get("verify_tls", True) else "ignore",
            read_timeout_sec=int(config.get("timeout_seconds") or 30) + 10,
            operation_timeout_sec=int(config.get("timeout_seconds") or 30),
        )
        result = session.run_ps(script)
        return (
            int(result.status_code),
            result.std_out.decode("utf-8", "replace"),
            result.std_err.decode("utf-8", "replace"),
        )

    return run


class WindowsWinRMCollector:
    def __init__(
        self,
        config: dict[str, Any],
        credentials: dict[str, Any],
        *,
        run_ps: RunPS | None = None,
    ) -> None:
        if not credentials.get("username") or not credentials.get("password"):
            self._missing = True
        else:
            self._missing = False
        use_https = bool(config.get("use_https", False))
        self.default_port = int(config.get("port") or (5986 if use_https else 5985))
        self.timeout = float(config.get("timeout_seconds") or 30)
        self.run_ps = run_ps or _pywinrm_runner(config, credentials)

    async def _run(self, target: Target, script: str) -> str:
        if self._missing:
            raise CollectorError("WinRM needs a username and password", code="missing_credentials")
        port = target.port or self.default_port
        try:
            status, out, err = await asyncio.wait_for(
                asyncio.to_thread(self.run_ps, target.host, port, script), self.timeout + 15
            )
        except TimeoutError as exc:
            raise CollectorError("WinRM request timed out", code="timeout") from exc
        except CollectorError:
            raise
        except Exception as exc:  # pywinrm/requests raise many unrelated types
            name = type(exc).__name__
            code = "auth_failed" if "Unauthorized" in name or "401" in str(exc) else "unreachable"
            raise CollectorError(
                f"WinRM connection failed: {name}: {exc}"[:500], code=code
            ) from exc
        if status != 0:
            raise CollectorError(f"PowerShell exited with status {status}: {err.strip()[:300]}")
        return out

    async def test(self, target: Target) -> TestOutcome:
        try:
            out = await self._run(target, TEST_SCRIPT)
        except CollectorError as exc:
            return TestOutcome(False, str(exc), {"code": exc.code})
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        return TestOutcome(True, f"Connected: {' / '.join(lines)[:300]}", {"system": lines})

    async def collect(self, target: Target) -> TargetResult:
        start = time.perf_counter()
        try:
            out = await self._run(target, WINDOWS_SCRIPT)
            try:
                raw = json.loads(out.strip())
            except ValueError as exc:
                raise CollectorError("The PowerShell script returned no JSON") from exc
            resource = parse_windows(raw, target.host)
        except CollectorError as exc:
            return TargetResult(target, False, error=str(exc), error_code=exc.code)
        if target.name:
            resource.name = target.name
        return TargetResult(
            target,
            True,
            resources=[resource],
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
