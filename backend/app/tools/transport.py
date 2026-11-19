"""How tools reach a managed resource, reusing the discovery transports.

A resource maps to its integration and discovery target. Containers are reached
through their host's integration (docker CLI over SSH, or the TLS Engine API).
SSH tools only run against hosts whose key is already pinned: diagnostics never
trust a key on first use; discovery or *Test* does that, visibly.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.integrations.base import CollectorError, Target
from app.integrations.docker import DockerTLSCollector
from app.integrations.ssh import CommandResult, SSHCredentials, ssh_session
from app.integrations.windows import _pywinrm_runner
from app.tools.base import ToolError


@dataclass(slots=True)
class Connection:
    """Everything needed to contact one resource. Never shown to a model."""

    kind: str  # "ssh" | "winrm" | "docker_tls"
    target: Target
    config: dict[str, Any]
    credentials: dict[str, Any]
    pinned_key: str | None = None
    container: str | None = None  # docker container name, for container resources


class Transport(Protocol):
    async def shell(self, command: str) -> CommandResult: ...

    async def powershell(self, script: str) -> str: ...

    async def docker_get(self, path: str) -> httpx.Response: ...


# Per-host concurrency: at most two diagnostic commands at a time per target.
_host_locks: dict[str, asyncio.Semaphore] = {}


def _host_slot(key: str) -> asyncio.Semaphore:
    if key not in _host_locks:
        _host_locks[key] = asyncio.Semaphore(2)
    return _host_locks[key]


class DefaultTransport:
    def __init__(self, conn: Connection, timeout: float) -> None:
        self.conn = conn
        self.timeout = timeout

    def _slot(self) -> asyncio.Semaphore:
        return _host_slot(f"{self.conn.kind}:{self.conn.target.label}")

    async def shell(self, command: str) -> CommandResult:
        if self.conn.kind != "ssh":
            raise ToolError("This resource is not reachable over SSH", code="unsupported")
        if not self.conn.pinned_key:
            raise ToolError(
                "The SSH host key of this target is not pinned yet. Run discovery or Test "
                "on its integration first.",
                code="host_key_not_pinned",
            )
        port = self.conn.target.port or int(self.conn.config.get("port") or 22)
        try:
            creds = SSHCredentials.from_dict(self.conn.credentials)
            async with (
                self._slot(),
                ssh_session(
                    self.conn.target.host,
                    port,
                    creds,
                    pinned_public_key=self.conn.pinned_key,
                    wait_seconds=min(self.timeout, 30),
                ) as session,
            ):
                return await asyncio.wait_for(session.run(command), self.timeout)
        except CollectorError as exc:
            raise ToolError(str(exc), code=exc.code) from exc
        except TimeoutError as exc:
            raise ToolError("The command timed out on the target", code="timeout") from exc

    async def powershell(self, script: str) -> str:
        if self.conn.kind != "winrm":
            raise ToolError("This resource is not reachable over WinRM", code="unsupported")
        if not self.conn.credentials.get("username") or not self.conn.credentials.get("password"):
            raise ToolError("WinRM needs a username and password", code="missing_credentials")
        use_https = bool(self.conn.config.get("use_https", False))
        port = self.conn.target.port or int(
            self.conn.config.get("port") or (5986 if use_https else 5985)
        )
        run = _pywinrm_runner(self.conn.config, self.conn.credentials)
        try:
            async with self._slot():
                status, out, err = await asyncio.wait_for(
                    asyncio.to_thread(run, self.conn.target.host, port, script),
                    self.timeout + 10,
                )
        except TimeoutError as exc:
            raise ToolError("WinRM request timed out", code="timeout") from exc
        except Exception as exc:  # pywinrm/requests raise many unrelated types
            raise ToolError(
                f"WinRM connection failed: {type(exc).__name__}: {exc}"[:400], code="unreachable"
            ) from exc
        if status != 0:
            raise ToolError(f"PowerShell exited with status {status}: {err.strip()[:300]}")
        return out

    async def docker_get(self, path: str) -> httpx.Response:
        if self.conn.kind != "docker_tls":
            raise ToolError("This resource has no Docker Engine API access", code="unsupported")
        collector = DockerTLSCollector(self.conn.config, self.conn.credentials)
        try:
            async with self._slot(), collector._client(self.conn.target) as client:
                return await client.get(path, timeout=self.timeout)
        except CollectorError as exc:
            raise ToolError(str(exc), code=exc.code) from exc
        except (httpx.HTTPError, ssl.SSLError, OSError) as exc:
            raise ToolError(
                f"Cannot reach the Docker API: {type(exc).__name__}", code="unreachable"
            ) from exc


TransportFactory = Callable[[Connection, float], Transport]
_factory: TransportFactory | None = None


def get_transport(conn: Connection, timeout: float) -> Transport:
    return (_factory or DefaultTransport)(conn, timeout)


def set_transport_factory(factory: TransportFactory | None) -> None:
    """Test seam: replace how tools reach hosts."""
    global _factory
    _factory = factory


def reset_host_slots() -> None:
    with contextlib.suppress(RuntimeError):
        _host_locks.clear()
