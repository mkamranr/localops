"""Network checks run from the LocalOps worker against inventoried resources only.

Targets come from the inventory (hostname, recorded IPs, or an HTTP service's URL),
never from free text, so these tools cannot be used to probe arbitrary addresses.
Results describe reachability *from LocalOps*, which the report states.
"""

from __future__ import annotations

import asyncio
import re
import socket
import ssl
import time
from typing import Literal

import httpx
from pydantic import Field, field_validator

from app.tools.base import ResourceArgs, ToolError, ToolOutput, ToolSpec
from app.tools.context import ToolContext

PATH_RE = re.compile(r"^/[A-Za-z0-9/._~%-]{0,200}$")


def _address(ctx: ToolContext) -> str:
    r = ctx.require_resource()
    if r.ip_addresses:
        return r.ip_addresses[0]
    if r.hostname:
        return r.hostname
    raise ToolError(f"{r.name} has no hostname or IP address in the inventory", code="no_address")


async def dns_lookup(ctx: ToolContext, args: ResourceArgs) -> ToolOutput:
    r = ctx.require_resource()
    name = r.hostname
    if not name and r.resource_type == "http_service":
        name = httpx.URL(str((r.facts.get("http") or {}).get("url") or "")).host or None
    if not name:
        raise ToolError(f"{r.name} has no hostname to resolve", code="no_address")
    loop = asyncio.get_running_loop()
    start = time.perf_counter()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(name, None), ctx.timeout)
    except (socket.gaierror, TimeoutError) as exc:
        return ToolOutput(
            f"{name}: resolution failed ({type(exc).__name__}: {exc})",
            f"DNS lookup of {name} FAILED from LocalOps",
            {"resolved": False},
        )
    ms = round((time.perf_counter() - start) * 1000, 1)
    resolved = sorted({str(i[4][0]) for i in infos})
    recorded = set(r.ip_addresses)
    mismatch = bool(recorded) and not (recorded & set(resolved))
    text = (
        f"{name} -> {', '.join(resolved)} in {ms} ms\n"
        f"inventory addresses: {', '.join(sorted(recorded)) or '(none)'}"
    )
    summary = f"{name} resolves to {', '.join(resolved[:3])}" + (
        " (does NOT match the inventory addresses)" if mismatch else ""
    )
    return ToolOutput(
        text, summary, {"resolved": True, "addresses": resolved, "mismatch": mismatch}
    )


class TcpArgs(ResourceArgs):
    port: int = Field(ge=1, le=65535)


async def tcp_check(ctx: ToolContext, args: TcpArgs) -> ToolOutput:
    host = _address(ctx)
    start = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, args.port), min(ctx.timeout, 10)
        )
        writer.close()
        ms = round((time.perf_counter() - start) * 1000, 1)
        return ToolOutput(
            f"TCP {host}:{args.port} open, connected in {ms} ms",
            f"Port {args.port} on {ctx.require_resource().name} is reachable from LocalOps",
            {"open": True, "latency_ms": ms},
        )
    except (OSError, TimeoutError) as exc:
        reason = "timed out" if isinstance(exc, TimeoutError) else str(exc) or type(exc).__name__
        return ToolOutput(
            f"TCP {host}:{args.port} not reachable: {reason}",
            f"Port {args.port} on {ctx.require_resource().name} is NOT reachable from LocalOps",
            {"open": False},
        )


class HttpArgs(ResourceArgs):
    path: str = Field("/", description="URL path for non-HTTP-service resources")
    port: int | None = Field(None, ge=1, le=65535)
    scheme: Literal["http", "https"] = "http"

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not PATH_RE.fullmatch(v):
            raise ValueError("path must start with / and contain only URL path characters")
        return v


async def http_check(ctx: ToolContext, args: HttpArgs) -> ToolOutput:
    r = ctx.require_resource()
    if r.resource_type == "http_service":
        url = str((r.facts.get("http") or {}).get("url") or "")
        if not url:
            raise ToolError("The HTTP service has no URL recorded", code="no_address")
    else:
        host = _address(ctx)
        host = f"[{host}]" if ":" in host else host
        port = f":{args.port}" if args.port else ""
        url = f"{args.scheme}://{host}{port}{args.path}"
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=min(ctx.timeout, 15), follow_redirects=False, trust_env=False
        ) as client:
            resp = await client.get(url, headers={"User-Agent": "LocalOps-diagnostics"})
    except (httpx.HTTPError, ssl.SSLError, OSError) as exc:
        return ToolOutput(
            f"GET {url} failed: {type(exc).__name__}: {exc}"[:500],
            f"HTTP request to {r.name} FAILED from LocalOps ({type(exc).__name__})",
            {"ok": False},
        )
    ms = round((time.perf_counter() - start) * 1000, 1)
    body = resp.text[:600]
    server = resp.headers.get("server")
    text = f"GET {url} -> HTTP {resp.status_code} in {ms} ms\nserver: {server}\n\n{body}"
    return ToolOutput(
        text,
        f"{r.name} answered HTTP {resp.status_code} in {ms} ms",
        {"status_code": resp.status_code, "latency_ms": ms},
    )


ANY_REMOTE = (
    "linux_server",
    "windows_server",
    "docker_host",
    "docker_container",
    "http_service",
    "application",
    "database",
    "network_device",
    "other",
)

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "net_dns_lookup",
        "Resolve a resource's hostname from LocalOps and compare with the inventory addresses.",
        ResourceArgs,
        dns_lookup,
        resource_types=ANY_REMOTE,
        category="network",
        timeout_seconds=10,
    ),
    ToolSpec(
        "net_tcp_check",
        "Check whether a TCP port on a resource is reachable from LocalOps.",
        TcpArgs,
        tcp_check,
        resource_types=ANY_REMOTE,
        category="network",
        timeout_seconds=12,
    ),
    ToolSpec(
        "net_http_check",
        "HTTP GET against an HTTP service (its configured URL) or a resource's address; "
        "returns status, latency and the start of the body.",
        HttpArgs,
        http_check,
        resource_types=ANY_REMOTE,
        category="network",
        timeout_seconds=20,
    ),
)
