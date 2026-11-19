"""Infrastructure collectors (Linux/SSH, Docker, Windows/WinRM, HTTP)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.integrations.base import Collector, CollectorError
from app.integrations.docker import DockerTLSCollector
from app.integrations.http_check import HTTPCollector
from app.integrations.linux_ssh import LinuxSSHCollector
from app.integrations.windows import WindowsWinRMCollector

# Tests swap this to inject fakes: (type, config, credentials, pins) -> Collector
CollectorFactory = Callable[
    [str, dict[str, Any], dict[str, Any], dict[tuple[str, int], str]], Collector
]


def build_collector(
    integration_type: str,
    config: dict[str, Any],
    credentials: dict[str, Any],
    pins: dict[tuple[str, int], str],
) -> Collector:
    if integration_type == "linux_ssh":
        return LinuxSSHCollector(config, credentials, pins)
    if integration_type == "docker":
        if config.get("mode", "ssh") == "tls":
            return DockerTLSCollector(config, credentials)
        return LinuxSSHCollector(config, credentials, pins, docker="required")
    if integration_type == "windows_winrm":
        return WindowsWinRMCollector(config, credentials)
    if integration_type == "http":
        return HTTPCollector(config)
    raise CollectorError(f"Unknown integration type {integration_type!r}", code="invalid_type")


_factory: CollectorFactory = build_collector


def get_collector_factory() -> CollectorFactory:
    return _factory


def set_collector_factory(factory: CollectorFactory | None) -> None:
    global _factory
    _factory = factory or build_collector


def uses_ssh(integration_type: str, config: dict[str, Any]) -> bool:
    return integration_type == "linux_ssh" or (
        integration_type == "docker" and config.get("mode", "ssh") != "tls"
    )
