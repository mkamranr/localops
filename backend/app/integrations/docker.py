"""Docker containers: parsing ``docker inspect`` / Engine API JSON, and the TLS collector.

The same container JSON comes from ``docker inspect`` over SSH and from the Engine
API's ``/containers/{id}/json``, so both modes share the parser.
"""

from __future__ import annotations

import json
import ssl
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from app.integrations.base import (
    CollectorError,
    DiscoveredResource,
    Target,
    TargetResult,
    TestOutcome,
    clean_ips,
)

# Fixed commands used in SSH mode (see linux_ssh.py). xargs -r keeps it a no-op when
# there are no containers; nothing from the target is interpolated into a command.
DOCKER_INFO_CMD = "docker info --format '{{json .}}'"
DOCKER_INSPECT_CMD = "docker ps -aq --no-trunc | head -n 500 | xargs -r docker inspect"

MAX_CONTAINERS = 500


def parse_json_documents(text: str) -> Any:
    text = text.strip()
    if not text:
        return []
    return json.loads(text)


def container_facts(raw: dict[str, Any]) -> dict[str, Any]:
    config = raw.get("Config") or {}
    state = raw.get("State") or {}
    host_config = raw.get("HostConfig") or {}
    network = raw.get("NetworkSettings") or {}
    labels = config.get("Labels") or {}
    ports: list[dict[str, Any]] = []
    for spec, bindings in (network.get("Ports") or {}).items():
        container_port, _, proto = spec.partition("/")
        for b in bindings or [{}]:
            ports.append(
                {
                    "container_port": int(container_port) if container_port.isdigit() else None,
                    "protocol": proto or "tcp",
                    "host_ip": (b or {}).get("HostIp") or None,
                    "host_port": int(b["HostPort"]) if (b or {}).get("HostPort") else None,
                }
            )
    networks = {
        name: (n or {}).get("IPAddress") or None
        for name, n in (network.get("Networks") or {}).items()
    }
    health = (state.get("Health") or {}).get("Status")
    return {
        "id": raw.get("Id", "")[:64],
        "name": str(raw.get("Name", "")).lstrip("/"),
        "image": config.get("Image"),
        "state": state.get("Status"),
        "health": health,
        "exit_code": state.get("ExitCode"),
        "started_at": state.get("StartedAt"),
        "finished_at": state.get("FinishedAt"),
        "restart_count": raw.get("RestartCount", 0),
        "restart_policy": (host_config.get("RestartPolicy") or {}).get("Name") or None,
        "ports": ports,
        "networks": networks,
        "compose": {
            "project": labels.get("com.docker.compose.project"),
            "service": labels.get("com.docker.compose.service"),
        },
        "mounts": len(raw.get("Mounts") or []),
    }


def container_resources(
    host_external_id: str, host_name: str, containers: list[dict[str, Any]]
) -> list[DiscoveredResource]:
    out: list[DiscoveredResource] = []
    for raw in containers[:MAX_CONTAINERS]:
        facts = container_facts(raw)
        if not facts["name"]:
            continue
        facts["collector"] = "docker"
        facts["host"] = host_name
        out.append(
            DiscoveredResource(
                external_id=f"container:{host_external_id}:{facts['name']}",
                name=facts["name"],
                resource_type="docker_container",
                hostname=None,
                ip_addresses=clean_ips([ip for ip in facts["networks"].values() if ip]),
                os=None,
                facts={"container": facts, "collector": "docker"},
                parent_external_id=host_external_id,
            )
        )
    return out


def docker_summary(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "server_version": info.get("ServerVersion"),
        "containers": info.get("Containers"),
        "running": info.get("ContainersRunning"),
        "stopped": info.get("ContainersStopped"),
        "images": info.get("Images"),
        "storage_driver": info.get("Driver"),
        "engine_id": info.get("ID"),
    }


# --------------------------------------------------------------------------- TLS mode


class DockerTLSCollector:
    """Docker Engine API over TCP with mutual TLS (``dockerd --tlsverify``)."""

    def __init__(self, config: dict[str, Any], credentials: dict[str, Any]) -> None:
        self.default_port = int(config.get("port") or 2376)
        self.timeout = float(config.get("timeout_seconds") or 20)
        self.verify_hostname = bool(config.get("verify_hostname", True))
        self.credentials = credentials

    def _ssl_context(self) -> ssl.SSLContext:
        ca, cert, key = (self.credentials.get(k) for k in ("ca", "cert", "key"))
        if not (ca and cert and key):
            raise CollectorError(
                "Docker TLS mode needs the CA certificate, client certificate and key",
                code="missing_credentials",
            )
        ctx = ssl.create_default_context(cadata=ca)
        ctx.check_hostname = self.verify_hostname
        # load_cert_chain only reads files; keep them in a private temp dir briefly.
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp, "cert.pem"), Path(tmp, "key.pem")
            cert_path.write_text(cert, encoding="utf-8")
            key_path.write_text(key, encoding="utf-8")
            key_path.chmod(0o600)
            ctx.load_cert_chain(str(cert_path), str(key_path))
        return ctx

    def _client(self, target: Target) -> httpx.AsyncClient:
        port = target.port or self.default_port
        return httpx.AsyncClient(
            base_url=f"https://{target.host}:{port}",
            verify=self._ssl_context(),
            timeout=self.timeout,
            trust_env=False,
        )

    async def _get(self, client: httpx.AsyncClient, path: str) -> Any:
        resp = await client.get(path)
        if resp.status_code >= 400:
            raise CollectorError(f"Docker API {path} returned HTTP {resp.status_code}")
        return resp.json()

    async def test(self, target: Target) -> TestOutcome:
        try:
            async with self._client(target) as client:
                version = await self._get(client, "/version")
        except CollectorError as exc:
            return TestOutcome(False, str(exc))
        except (httpx.HTTPError, ssl.SSLError, OSError) as exc:
            return TestOutcome(False, f"Cannot reach Docker API: {type(exc).__name__}: {exc}")
        return TestOutcome(
            True,
            f"Docker {version.get('Version')} (API {version.get('ApiVersion')})",
            {"version": version.get("Version")},
        )

    async def collect(self, target: Target) -> TargetResult:
        start = time.perf_counter()
        try:
            async with self._client(target) as client:
                info = await self._get(client, "/info")
                listing = await self._get(client, "/containers/json?all=1")
                containers = [
                    await self._get(client, f"/containers/{c['Id']}/json")
                    for c in listing[:MAX_CONTAINERS]
                    if str(c.get("Id", "")).isalnum()
                ]
        except CollectorError as exc:
            return TargetResult(target, False, error=str(exc), error_code=exc.code)
        except (httpx.HTTPError, ssl.SSLError, OSError, ValueError) as exc:
            return TargetResult(
                target, False, error=f"{type(exc).__name__}: {exc}", error_code="unreachable"
            )
        engine_id = info.get("ID") or target.host
        host_ext = f"dockerd:{engine_id}"
        name = target.name or info.get("Name") or target.host
        host = DiscoveredResource(
            external_id=host_ext,
            name=name,
            resource_type="docker_host",
            hostname=info.get("Name"),
            ip_addresses=clean_ips([target.host]),
            os=info.get("OperatingSystem"),
            facts={
                "collector": "docker_tls",
                "docker": docker_summary(info),
                "cpu_count": info.get("NCPU"),
                "memory": {"total_mb": round((info.get("MemTotal") or 0) / 1024 / 1024) or None},
                "kernel": info.get("KernelVersion"),
            },
            metrics={"cpu_count": info.get("NCPU")},
        )
        resources = [host, *container_resources(host_ext, name, containers)]
        return TargetResult(
            target,
            True,
            resources=resources,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
