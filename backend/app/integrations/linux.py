"""Linux host facts: the fixed collection script and pure parsers for its output."""

from __future__ import annotations

import re
from typing import Any

from app.integrations.base import DiscoveredResource, clean_ips

# One round trip; every command is read-only and needs no privileges. Sections are
# delimited by marker lines. Nothing is interpolated into this script.
LINUX_SCRIPT = r"""
export LC_ALL=C
echo '@@hostname'; hostname 2>/dev/null
echo '@@fqdn'; hostname -f 2>/dev/null
echo '@@machine_id'; cat /etc/machine-id 2>/dev/null
echo '@@os_release'; cat /etc/os-release 2>/dev/null
echo '@@kernel'; uname -srm 2>/dev/null
echo '@@uptime'; cat /proc/uptime 2>/dev/null
echo '@@loadavg'; cat /proc/loadavg 2>/dev/null
echo '@@nproc'; nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null
echo '@@meminfo'; cat /proc/meminfo 2>/dev/null
echo '@@df'; df -PTk -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs 2>/dev/null
echo '@@ip'; ip -o addr show 2>/dev/null
echo '@@services'; systemctl list-units --type=service --state=running \
  --no-pager --no-legend --plain 2>/dev/null
echo '@@failed'; systemctl list-units --type=service --state=failed \
  --no-pager --no-legend --plain 2>/dev/null
echo '@@listen'; ss -ltnH 2>/dev/null
echo '@@established'; ss -tnH state established 2>/dev/null
echo '@@docker'; command -v docker >/dev/null 2>&1 && echo present
echo '@@end'
"""

MAX_ITEMS = 200


def split_sections(output: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in output.splitlines():
        if line.startswith("@@") and re.fullmatch(r"@@[a-z_]+", line.strip()):
            current = line.strip()[2:]
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def parse_os_release(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def parse_meminfo(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def parse_df(text: str) -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 7 or not parts[2].isdigit():
            continue
        fs, fstype, total, used, _avail, capacity = parts[:6]
        mount = " ".join(parts[6:])
        try:
            pct = float(capacity.rstrip("%"))
        except ValueError:
            continue
        disks.append(
            {
                "mount": mount,
                "filesystem": fs,
                "type": fstype,
                "size_gb": round(int(total) / 1024 / 1024, 1),
                "used_gb": round(int(used) / 1024 / 1024, 1),
                "used_percent": pct,
            }
        )
    return disks[:MAX_ITEMS]


def parse_ip_addr(text: str) -> list[dict[str, Any]]:
    interfaces: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] not in ("inet", "inet6"):
            continue
        address, _, prefix = parts[3].partition("/")
        interfaces.append(
            {
                "name": parts[1].rstrip(":"),
                "family": "ipv4" if parts[2] == "inet" else "ipv6",
                "address": address,
                "prefix": int(prefix) if prefix.isdigit() else None,
            }
        )
    return interfaces[:MAX_ITEMS]


def parse_systemd_units(text: str) -> list[dict[str, str]]:
    units: list[dict[str, str]] = []
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4 or not parts[0].endswith(".service"):
            continue
        units.append(
            {
                "name": parts[0].removesuffix(".service"),
                "state": parts[3],
                "description": parts[4] if len(parts) > 4 else "",
            }
        )
    return units[: MAX_ITEMS * 2]


_ADDR = re.compile(r"^(?P<host>.+):(?P<port>\d+|\*)$")


def split_address(token: str) -> tuple[str, int | None] | None:
    """Parse ``ss`` addresses: ``0.0.0.0:22``, ``[::]:22``, ``*:80``, ``[::ffff:1.2.3.4]:5``."""
    m = _ADDR.match(token)
    if not m:
        return None
    host = m.group("host").strip("[]").split("%", 1)[0]
    if host.lower().startswith("::ffff:"):
        host = host[7:]
    port = int(m.group("port")) if m.group("port").isdigit() else None
    return host, port


def _addresses(line: str) -> list[tuple[str, int | None]]:
    found = []
    for token in line.split():
        parsed = split_address(token)
        if parsed:
            found.append(parsed)
    return found


def parse_listen(text: str) -> list[dict[str, Any]]:
    ports: dict[int, dict[str, Any]] = {}
    for line in text.splitlines():
        addrs = _addresses(line)
        if not addrs or addrs[0][1] is None:
            continue
        host, port = addrs[0]
        assert port is not None  # noqa: S101 - checked above
        entry = ports.setdefault(port, {"port": port, "addresses": []})
        if host not in entry["addresses"]:
            entry["addresses"].append(host)
    return [ports[p] for p in sorted(ports)][:MAX_ITEMS]


def parse_established(text: str, listen_ports: set[int]) -> dict[str, list[dict[str, Any]]]:
    """Group established TCP connections into outbound (we are the client) and inbound."""
    outbound: dict[tuple[str, int], int] = {}
    inbound: dict[tuple[str, int], int] = {}
    for line in text.splitlines():
        addrs = _addresses(line)
        if len(addrs) < 2:
            continue
        (_lhost, lport), (rhost, rport) = addrs[0], addrs[1]
        if lport is None or rport is None or not clean_ips([rhost]):
            continue
        if lport in listen_ports:
            inbound[(rhost, lport)] = inbound.get((rhost, lport), 0) + 1
        else:
            outbound[(rhost, rport)] = outbound.get((rhost, rport), 0) + 1
    return {
        "outbound": [
            {"remote_ip": h, "remote_port": p, "count": c} for (h, p), c in sorted(outbound.items())
        ][:MAX_ITEMS],
        "inbound": [
            {"remote_ip": h, "local_port": p, "count": c} for (h, p), c in sorted(inbound.items())
        ][:MAX_ITEMS],
    }


def _float(text: str, index: int = 0) -> float | None:
    try:
        return float(text.split()[index])
    except (IndexError, ValueError):
        return None


def parse_linux(output: str, target_host: str) -> tuple[DiscoveredResource, bool]:
    """Build the host resource. Returns (resource, docker_cli_present)."""
    sec = split_sections(output)
    os_release = parse_os_release(sec.get("os_release", ""))
    mem = parse_meminfo(sec.get("meminfo", ""))
    disks = parse_df(sec.get("df", ""))
    interfaces = parse_ip_addr(sec.get("ip", ""))
    listen = parse_listen(sec.get("listen", ""))
    connections = parse_established(sec.get("established", ""), {p["port"] for p in listen})
    running = parse_systemd_units(sec.get("services", ""))
    failed = parse_systemd_units(sec.get("failed", ""))

    hostname = (sec.get("fqdn") or sec.get("hostname") or "").splitlines()
    host_name = (sec.get("hostname") or "").strip() or target_host
    machine_id = (sec.get("machine_id") or "").strip()
    cpu_count = int(sec["nproc"]) if sec.get("nproc", "").strip().isdigit() else None
    load = [v for v in (_float(sec.get("loadavg", ""), i) for i in range(3)) if v is not None]
    total_kb = mem.get("MemTotal")
    avail_kb = mem.get("MemAvailable", mem.get("MemFree"))
    memory_percent = (
        round((total_kb - avail_kb) / total_kb * 100, 1)
        if total_kb and avail_kb is not None
        else None
    )
    disk_percent = max((d["used_percent"] for d in disks), default=None)
    pretty = os_release.get("PRETTY_NAME") or os_release.get("NAME")
    ips = clean_ips([i["address"] for i in interfaces])

    facts: dict[str, Any] = {
        "collector": "linux_ssh",
        "os": {
            "name": pretty,
            "id": os_release.get("ID"),
            "version": os_release.get("VERSION_ID"),
        },
        "kernel": sec.get("kernel") or None,
        "uptime_seconds": int(_float(sec.get("uptime", "")) or 0) or None,
        "load": load,
        "cpu_count": cpu_count,
        "memory": {
            "total_mb": round(total_kb / 1024) if total_kb else None,
            "available_mb": round(avail_kb / 1024) if avail_kb is not None else None,
            "used_percent": memory_percent,
        },
        "disks": disks,
        "interfaces": interfaces,
        "services": [{"name": u["name"], "description": u["description"]} for u in running],
        "failed_services": [u["name"] for u in failed],
        "listening_ports": listen,
        "connections": connections,
    }
    resource = DiscoveredResource(
        external_id=f"host:{machine_id}"
        if re.fullmatch(r"[0-9a-f]{32}", machine_id)
        else (f"host:{target_host.lower()}"),
        name=host_name,
        resource_type="linux_server",
        hostname=(hostname[0].strip() if hostname and hostname[0].strip() else host_name),
        ip_addresses=ips,
        os=pretty,
        facts=facts,
        metrics={
            "cpu_load": load[0] if load else None,
            "cpu_count": cpu_count,
            "memory_percent": memory_percent,
            "disk_percent": disk_percent,
        },
    )
    return resource, sec.get("docker", "").strip() == "present"
