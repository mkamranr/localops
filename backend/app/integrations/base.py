"""Shared types for infrastructure collectors.

Collectors run a *fixed* set of read-only commands (no free-form input reaches a
shell), return normalised facts and never see the LLM. Credentials stay inside the
collector object; facts must never contain them.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any, Protocol

MAX_OUTPUT_BYTES = 2 * 1024 * 1024


class CollectorError(Exception):
    """A target could not be collected. ``code`` is machine-readable."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "collection_failed",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class Target:
    host: str
    port: int | None = None
    name: str | None = None

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port}" if self.port else self.host

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Target:
        port = raw.get("port")
        return cls(
            host=str(raw["host"]).strip(),
            port=int(port) if port else None,
            name=(str(raw["name"]).strip() or None) if raw.get("name") else None,
        )


@dataclass(slots=True)
class DiscoveredResource:
    external_id: str
    name: str
    resource_type: str
    hostname: str | None = None
    ip_addresses: list[str] = field(default_factory=list)
    os: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    # Optional point-in-time metrics (cpu_load, cpu_count, memory_percent, ...).
    metrics: dict[str, float | int | None] | None = None
    # Container -> its host, by external id.
    parent_external_id: str | None = None


@dataclass(slots=True)
class HostKeyObservation:
    host: str
    port: int
    key_type: str
    public_key: str
    fingerprint: str


@dataclass(slots=True)
class TargetResult:
    target: Target
    ok: bool
    resources: list[DiscoveredResource] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    duration_ms: float = 0.0
    host_key: HostKeyObservation | None = None
    # e.g. the fingerprint presented when a pinned key no longer matches
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TestOutcome:
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    host_key: HostKeyObservation | None = None


class Collector(Protocol):
    async def collect(self, target: Target) -> TargetResult: ...

    async def test(self, target: Target) -> TestOutcome: ...


def clean_ips(values: list[str]) -> list[str]:
    """Valid, non-loopback, non-link-local addresses, de-duplicated in order."""
    out: list[str] = []
    for value in values:
        try:
            ip = ipaddress.ip_address(value.split("%", 1)[0].strip())
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            continue
        text = str(ip)
        if text not in out:
            out.append(text)
    return out


def truncate(text: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    return text if len(text) <= limit else text[:limit]
