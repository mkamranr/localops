"""HTTP(S) endpoint checks: status, latency, TLS certificate expiry, resolved IPs."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography import x509

from app.integrations.base import (
    CollectorError,
    DiscoveredResource,
    Target,
    TargetResult,
    TestOutcome,
    clean_ips,
)


def normalise_url(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        value = f"https://{value}"
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CollectorError(f"Not an http(s) URL: {value!r}", code="invalid_target")
    return value


class HTTPCollector:
    def __init__(
        self, config: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.timeout = float(config.get("timeout_seconds") or 10)
        self.verify_tls = bool(config.get("verify_tls", True))
        self.expected = [int(s) for s in config.get("expected_status") or []]
        self.slow_ms = float(config.get("slow_ms") or 2000)
        self._transport = transport

    def _status_ok(self, status: int) -> bool:
        return status in self.expected if self.expected else 200 <= status < 400

    async def _probe(self, url: str) -> dict[str, Any]:
        start = time.perf_counter()
        async with httpx.AsyncClient(
            timeout=self.timeout,
            verify=self.verify_tls,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        ) as client:
            resp = await client.get(url, headers={"User-Agent": "LocalOps-HealthCheck"})
        return {
            "status_code": resp.status_code,
            "latency_ms": round((time.perf_counter() - start) * 1000, 1),
            "server": resp.headers.get("server"),
        }

    async def test(self, target: Target) -> TestOutcome:
        try:
            url = normalise_url(target.host)
            probe = await self._probe(url)
        except CollectorError as exc:
            return TestOutcome(False, str(exc))
        except httpx.HTTPError as exc:
            return TestOutcome(False, f"{type(exc).__name__}: {exc}"[:300])
        ok = self._status_ok(probe["status_code"])
        return TestOutcome(ok, f"HTTP {probe['status_code']} in {probe['latency_ms']} ms", probe)

    async def collect(self, target: Target) -> TargetResult:
        start = time.perf_counter()
        try:
            url = normalise_url(target.host)
        except CollectorError as exc:
            return TargetResult(target, False, error=str(exc), error_code=exc.code)
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        facts: dict[str, Any] = {
            "collector": "http",
            "http": {"url": url, "expected_status": self.expected or "2xx/3xx"},
        }
        facts["http"]["resolved_ips"] = await _resolve(host, port)
        facts["http"]["port"] = port
        try:
            probe = await self._probe(url)
            facts["http"].update(probe)
            facts["http"]["ok"] = self._status_ok(probe["status_code"])
        except httpx.HTTPError as exc:
            facts["http"].update(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300], "status_code": None}
            )
        if parts.scheme == "https":
            facts["http"]["tls"] = await _certificate(host, port, self.timeout)
        facts["http"]["slow_ms"] = self.slow_ms
        resource = DiscoveredResource(
            external_id=f"http:{url}",
            name=target.name or f"{host}{parts.path if parts.path not in ('', '/') else ''}",
            resource_type="http_service",
            hostname=host,
            ip_addresses=clean_ips(facts["http"]["resolved_ips"]),
            facts=facts,
            metrics={"latency_ms": facts["http"].get("latency_ms")},
        )
        return TargetResult(
            target,
            True,
            resources=[resource],
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )


async def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return list(dict.fromkeys(str(i[4][0]) for i in infos))


async def _certificate(host: str, port: int, wait_seconds: float) -> dict[str, Any] | None:
    """Read the peer certificate (even if untrusted) to report its expiry."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # inspection only; the HTTP probe verifies trust
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=host), wait_seconds
        )
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        if not der:
            return None
        cert = x509.load_der_x509_certificate(der)
        expires = cert.not_valid_after_utc
        return {
            "subject": cert.subject.rfc4514_string()[:200],
            "issuer": cert.issuer.rfc4514_string()[:200],
            "expires_at": expires.isoformat(),
            "days_left": (expires - datetime.now(UTC)).days,
        }
    except (OSError, TimeoutError, ssl.SSLError, ValueError):
        return None
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, ssl.SSLError):
                await writer.wait_closed()
