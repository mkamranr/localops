"""Linux hosts over SSH, optionally with their Docker containers (Docker via SSH)."""

from __future__ import annotations

import time
from typing import Any

from app.integrations.base import (
    CollectorError,
    HostKeyObservation,
    Target,
    TargetResult,
    TestOutcome,
    clean_ips,
)
from app.integrations.docker import (
    DOCKER_INFO_CMD,
    DOCKER_INSPECT_CMD,
    container_resources,
    docker_summary,
    parse_json_documents,
)
from app.integrations.linux import LINUX_SCRIPT, parse_linux
from app.integrations.ssh import SSHCredentials, SSHSession, ssh_session

TEST_COMMAND = "uname -srm; hostname"


class LinuxSSHCollector:
    """``docker``: ``auto`` (collect containers when the CLI is usable), ``off``, or
    ``required`` (Docker integrations in SSH mode: report when Docker is unusable)."""

    def __init__(
        self,
        config: dict[str, Any],
        credentials: dict[str, Any],
        pins: dict[tuple[str, int], str],
        *,
        docker: str | None = None,
    ) -> None:
        self.default_port = int(config.get("port") or 22)
        self.timeout = float(config.get("timeout_seconds") or 20)
        self.docker = docker or str(config.get("docker") or "auto")
        self.credentials = credentials
        self.pins = pins

    def _creds(self) -> SSHCredentials:
        return SSHCredentials.from_dict(self.credentials)

    def _port(self, target: Target) -> int:
        return target.port or self.default_port

    async def test(self, target: Target) -> TestOutcome:
        port = self._port(target)
        observed: list[HostKeyObservation] = []
        try:
            async with ssh_session(
                target.host,
                port,
                self._creds(),
                pinned_public_key=self.pins.get((target.host, port)),
                wait_seconds=self.timeout,
                observed=observed,
            ) as session:
                result = await session.run(TEST_COMMAND)
                details: dict[str, Any] = {"system": result.stdout.strip()[:300]}
                if self.docker != "off":
                    details["docker"] = await self._docker_status(session)
        except CollectorError as exc:
            return TestOutcome(False, str(exc), {"code": exc.code, **exc.details})
        key = observed[0] if observed else None
        message = f"Connected as {self.credentials.get('username')}: {details['system']}"
        if key:
            details["host_key_fingerprint"] = key.fingerprint
            details["host_key_pinned"] = (target.host, port) in self.pins
        return TestOutcome(True, message, details, host_key=key)

    async def _docker_status(self, session: SSHSession) -> str:
        result = await session.run(DOCKER_INFO_CMD)
        if result.exit_status == 0:
            return "accessible"
        err = (result.stderr or result.stdout).lower()
        if "not found" in err:
            return "not installed"
        if "permission denied" in err:
            return "permission denied (user is not in the docker group)"
        return "unavailable"

    async def collect(self, target: Target) -> TargetResult:
        start = time.perf_counter()
        port = self._port(target)
        observed: list[HostKeyObservation] = []
        try:
            async with ssh_session(
                target.host,
                port,
                self._creds(),
                pinned_public_key=self.pins.get((target.host, port)),
                wait_seconds=self.timeout,
                observed=observed,
            ) as session:
                result = await session.run("sh -s", stdin=LINUX_SCRIPT)
                if "@@end" not in result.stdout:
                    raise CollectorError(
                        "The collection script did not complete (is the login shell POSIX?)"
                    )
                host, docker_cli = parse_linux(result.stdout, target.host)
                if target.name:
                    host.name = target.name
                resources = [host]
                if docker_cli and self.docker != "off":
                    resources += await self._collect_docker(session, host)
                elif self.docker == "required":
                    host.facts["docker"] = {"installed": False}
        except CollectorError as exc:
            return TargetResult(
                target,
                False,
                error=str(exc),
                error_code=exc.code,
                details=exc.details,
                duration_ms=round((time.perf_counter() - start) * 1000, 1),
            )
        # Make sure the address we connect to is matchable (NAT, floating IPs).
        host.ip_addresses = clean_ips([*host.ip_addresses, target.host])
        return TargetResult(
            target,
            True,
            resources=resources,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
            host_key=observed[0] if observed else None,
        )

    async def _collect_docker(self, session: SSHSession, host: Any) -> list[Any]:
        info_result = await session.run(DOCKER_INFO_CMD)
        if info_result.exit_status != 0:
            status = (
                "permission denied"
                if "permission denied" in (info_result.stderr.lower())
                else "unavailable"
            )
            host.facts["docker"] = {"installed": True, "accessible": False, "status": status}
            return []
        try:
            info = parse_json_documents(info_result.stdout)
            inspect = await session.run(DOCKER_INSPECT_CMD)
            containers = parse_json_documents(inspect.stdout) if inspect.exit_status == 0 else []
        except ValueError:
            host.facts["docker"] = {"installed": True, "accessible": True, "status": "unparseable"}
            return []
        host.facts["docker"] = {
            "installed": True,
            "accessible": True,
            **docker_summary(info if isinstance(info, dict) else {}),
        }
        return container_resources(host.external_id, host.name, containers or [])
