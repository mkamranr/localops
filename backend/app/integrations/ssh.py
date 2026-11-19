"""SSH transport (asyncssh) with trust-on-first-use host-key pinning.

* No pin stored: connect, record the presented host key (the caller pins it).
* Pin stored: the pinned key is the *only* trusted key; asyncssh then also offers only
  that key's algorithm. A different key fails with ``host_key_changed`` and nothing
  is executed on the host. An administrator must reset the pin to accept a new key.

Neither ~/.ssh/config, default identity files nor an SSH agent are used: only the
credentials stored with the integration.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import asyncssh

from app.integrations.base import CollectorError, HostKeyObservation, truncate


@dataclass(frozen=True, slots=True)
class SSHCredentials:
    username: str
    password: str | None = None
    private_key: str | None = None
    passphrase: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SSHCredentials:
        username = str(raw.get("username") or "").strip()
        if not username:
            raise CollectorError("SSH username is not configured", code="missing_credentials")
        if not raw.get("password") and not raw.get("private_key"):
            raise CollectorError(
                "SSH needs a password or a private key", code="missing_credentials"
            )
        return cls(
            username=username,
            password=raw.get("password") or None,
            private_key=raw.get("private_key") or None,
            passphrase=raw.get("passphrase") or None,
        )


@dataclass(slots=True)
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str


def observe(host: str, port: int, key: asyncssh.SSHKey) -> HostKeyObservation:
    return HostKeyObservation(
        host=host,
        port=port,
        key_type=key.get_algorithm(),
        public_key=key.export_public_key("openssh").decode().strip(),
        fingerprint=key.get_fingerprint("sha256"),
    )


class SSHSession:
    def __init__(self, conn: asyncssh.SSHClientConnection, timeout: float) -> None:
        self._conn = conn
        self._timeout = timeout

    async def run(self, command: str, *, stdin: str | None = None) -> CommandResult:
        try:
            result = await self._conn.run(command, input=stdin, check=False, timeout=self._timeout)
        except (asyncssh.TimeoutError, TimeoutError) as exc:
            raise CollectorError("Command timed out on the target", code="timeout") from exc
        except asyncssh.Error as exc:
            raise CollectorError(f"SSH command failed: {exc}", code="ssh_error") from exc
        return CommandResult(
            exit_status=result.exit_status or 0,
            stdout=truncate(str(result.stdout or "")),
            stderr=truncate(str(result.stderr or ""), 4000),
        )


@asynccontextmanager
async def ssh_session(
    host: str,
    port: int,
    credentials: SSHCredentials,
    *,
    pinned_public_key: str | None,
    wait_seconds: float = 20.0,
    observed: list[HostKeyObservation] | None = None,
) -> AsyncIterator[SSHSession]:
    """Open an authenticated session; appends the server's key to ``observed``."""
    options: dict[str, Any] = {
        "username": credentials.username,
        "config": [],
        "agent_path": None,
        "connect_timeout": wait_seconds,
        "login_timeout": wait_seconds,
        "keepalive_interval": 0,
    }
    if credentials.private_key:
        try:
            key = asyncssh.import_private_key(credentials.private_key, credentials.passphrase)
        except (asyncssh.KeyImportError, ValueError) as exc:
            raise CollectorError(
                "The stored SSH private key cannot be loaded (wrong passphrase?)",
                code="invalid_credentials",
            ) from exc
        options["client_keys"] = [key]
        options["password"] = credentials.password
    else:
        options["client_keys"] = None
        options["password"] = credentials.password
        options["preferred_auth"] = "password,keyboard-interactive"
    if pinned_public_key:
        try:
            pinned = asyncssh.import_public_key(pinned_public_key)
        except (asyncssh.KeyImportError, ValueError) as exc:
            raise CollectorError("Stored host key is unreadable", code="host_key_invalid") from exc
        options["known_hosts"] = ([pinned], [], [])
    else:
        options["known_hosts"] = None
    try:
        conn = await asyncssh.connect(host, port, **options)
    except asyncssh.HostKeyNotVerifiable as exc:
        presented = await _presented_key(host, port, wait_seconds)
        raise CollectorError(
            "The SSH host key of this target changed since it was pinned. Verify the host, "
            "then reset its pinned key under Tools & Integrations to accept the new key.",
            code="host_key_changed",
            details={
                "presented_fingerprint": presented.fingerprint if presented else None,
                "presented_key_type": presented.key_type if presented else None,
            },
        ) from exc
    except asyncssh.PermissionDenied as exc:
        raise CollectorError("SSH authentication failed for this user", code="auth_failed") from exc
    except TimeoutError as exc:
        raise CollectorError("SSH connection timed out", code="unreachable") from exc
    except (OSError, asyncssh.Error) as exc:
        raise CollectorError(f"SSH connection failed: {exc}", code="unreachable") from exc
    try:
        server_key = conn.get_server_host_key()
        if server_key is not None and observed is not None:
            observed.append(observe(host, port, server_key))
        yield SSHSession(conn, wait_seconds)
    finally:
        conn.close()
        with contextlib.suppress(TimeoutError, asyncssh.Error, OSError):
            await asyncio.wait_for(conn.wait_closed(), 5)


async def _presented_key(host: str, port: int, wait_seconds: float) -> HostKeyObservation | None:
    try:
        key = await asyncio.wait_for(
            asyncssh.get_server_host_key(host, port, config=[]), wait_seconds
        )
    except (TimeoutError, OSError, asyncssh.Error):
        return None
    return observe(host, port, key) if key is not None else None
