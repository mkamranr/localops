"""Collector parsers, health rules, relationship derivation and the SSH transport."""

from __future__ import annotations

import uuid
from typing import Any

import asyncssh
import httpx
import pytest

from app.discovery import health
from app.discovery.relationships import Node, derive
from app.integrations.base import Target
from app.integrations.docker import container_facts, container_resources
from app.integrations.http_check import HTTPCollector
from app.integrations.linux import parse_linux, split_address
from app.integrations.linux_ssh import LinuxSSHCollector
from app.integrations.windows import WindowsWinRMCollector, parse_windows
from tests.inventory_fixtures import (
    CONTAINERS,
    LINUX_OUTPUT,
    MACHINE_ID,
    WINDOWS_JSON,
    FakeHost,
    port_of,
    start_ssh_server,
)

# ------------------------------------------------------------------ parsers


def test_parse_linux_host() -> None:
    host, docker = parse_linux(LINUX_OUTPUT, "10.0.0.11")
    assert docker is True
    assert host.external_id == f"host:{MACHINE_ID}"
    assert host.name == "app-01" and host.hostname == "app-01.corp.local"
    assert host.os == "Ubuntu 24.04.1 LTS"
    assert host.ip_addresses == ["10.0.0.11"]  # loopback and link-local dropped
    f = host.facts
    assert f["cpu_count"] == 4 and f["load"] == [0.42, 0.35, 0.3]
    assert f["memory"]["used_percent"] == 75.0
    assert [d["mount"] for d in f["disks"]] == ["/", "/data files"]
    assert f["disks"][0]["used_percent"] == 93.0
    assert {s["name"] for s in f["services"]} == {"ssh", "nginx", "docker"}
    assert f["failed_services"] == ["backup"]
    assert [p["port"] for p in f["listening_ports"]] == [22, 80, 8080]
    out = f["connections"]["outbound"]
    assert out == [{"remote_ip": "10.0.0.12", "remote_port": 5432, "count": 2}]
    inbound = {(c["remote_ip"], c["local_port"]) for c in f["connections"]["inbound"]}
    assert inbound == {("10.0.0.99", 22), ("10.0.0.50", 80)}
    assert host.metrics == {
        "cpu_load": 0.42,
        "cpu_count": 4,
        "memory_percent": 75.0,
        "disk_percent": 93.0,
    }


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("0.0.0.0:22", ("0.0.0.0", 22)),
        ("[::]:22", ("::", 22)),
        ("*:80", ("*", 80)),
        ("[::ffff:10.0.0.5]:5432", ("10.0.0.5", 5432)),
        ("10.0.0.5%eth0:53", ("10.0.0.5", 53)),
        ("0.0.0.0:*", ("0.0.0.0", None)),
        ("LISTEN", None),
    ],
)
def test_split_address(token: str, expected: Any) -> None:
    assert split_address(token) == expected


def test_container_facts_and_resources() -> None:
    facts = container_facts(CONTAINERS[0])
    assert facts["name"] == "web" and facts["state"] == "running" and facts["health"] == "healthy"
    assert facts["ports"] == [
        {"container_port": 80, "protocol": "tcp", "host_ip": "0.0.0.0", "host_port": 8080}
    ]
    assert facts["compose"] == {"project": "shop", "service": "web"}
    resources = container_resources("host:abc", "app-01", CONTAINERS)
    assert [r.external_id for r in resources] == [
        "container:host:abc:web",
        "container:host:abc:migrate",
    ]
    assert all(r.parent_external_id == "host:abc" for r in resources)


def test_parse_windows() -> None:
    r = parse_windows(WINDOWS_JSON, "10.0.0.20")
    assert r.resource_type == "windows_server"
    assert r.external_id == "host:4c4c4544-0042-3510-8051-b4c04f4e4d32"
    assert r.hostname == "win-fs01.corp.local"
    assert r.ip_addresses == ["10.0.0.20"]
    assert r.facts["disks"][0]["used_percent"] == 95.0
    assert r.facts["stopped_auto_services"] == ["Spooler"]
    assert r.facts["connections"]["inbound"] == [
        {"remote_ip": "10.0.0.11", "local_port": 445, "count": 1}
    ]
    assert r.metrics and r.metrics["memory_percent"] == 50.0


async def test_windows_collector_with_fake_runner() -> None:
    import json

    seen: list[tuple[str, int]] = []

    def run_ps(host: str, port: int, script: str) -> tuple[int, str, str]:
        seen.append((host, port))
        assert "ConvertTo-Json" in script or "COMPUTERNAME" in script
        return 0, json.dumps(WINDOWS_JSON), ""

    collector = WindowsWinRMCollector({}, {"username": "u", "password": "p"}, run_ps=run_ps)
    result = await collector.collect(Target("10.0.0.20", name="File server"))
    assert result.ok and result.resources[0].name == "File server"
    assert seen == [("10.0.0.20", 5985)]

    def failing(host: str, port: int, script: str) -> tuple[int, str, str]:
        raise ConnectionError("refused")

    bad = WindowsWinRMCollector({}, {"username": "u", "password": "p"}, run_ps=failing)
    result = await bad.collect(Target("10.0.0.21"))
    assert not result.ok and result.error_code == "unreachable"
    missing = await WindowsWinRMCollector({}, {}, run_ps=run_ps).test(Target("x"))
    assert not missing.ok and "username and password" in missing.message


async def test_http_collector() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503 if request.url.path == "/down" else 200)

    collector = HTTPCollector({"slow_ms": 5000}, transport=httpx.MockTransport(handler))
    ok = await collector.collect(Target("http://127.0.0.1:8080/health"))
    assert ok.ok
    r = ok.resources[0]
    assert (
        r.resource_type == "http_service" and r.external_id == "http:http://127.0.0.1:8080/health"
    )
    assert r.facts["http"]["ok"] is True and r.facts["http"]["port"] == 8080
    assert r.ip_addresses == ["127.0.0.1"] or r.facts["http"]["resolved_ips"] == ["127.0.0.1"]
    assert health.evaluate("http_service", r.facts) == ("healthy", [])
    down = (await collector.collect(Target("http://127.0.0.1:8080/down"))).resources[0]
    assert health.evaluate("http_service", down.facts)[0] == "unhealthy"


# ------------------------------------------------------------------ health


def test_host_health_rules() -> None:
    host, _ = parse_linux(LINUX_OUTPUT, "10.0.0.11")
    status, reasons = health.evaluate("linux_server", host.facts)
    assert status == "degraded"
    assert "Disk / is 93% full" in reasons
    assert any("Failed services: backup" in r for r in reasons)
    host.facts["disks"][0]["used_percent"] = 99
    assert health.evaluate("linux_server", host.facts)[0] == "unhealthy"


@pytest.mark.parametrize(
    ("container", "expected"),
    [
        ({"state": "running", "health": "healthy"}, "healthy"),
        ({"state": "running", "health": "unhealthy"}, "unhealthy"),
        ({"state": "running", "restart_count": 9}, "degraded"),
        ({"state": "restarting"}, "unhealthy"),
        ({"state": "exited", "exit_code": 0}, "unknown"),
        ({"state": "exited", "exit_code": 137}, "unhealthy"),
        ({"state": "paused"}, "degraded"),
    ],
)
def test_container_health(container: dict[str, Any], expected: str) -> None:
    assert health.evaluate("docker_container", {"container": container})[0] == expected


def test_http_health_tls_and_latency() -> None:
    facts = {"http": {"ok": True, "status_code": 200, "latency_ms": 3000, "slow_ms": 2000}}
    assert health.evaluate("http_service", facts)[0] == "degraded"
    facts["http"].update(latency_ms=10, tls={"days_left": -1})
    assert health.evaluate("http_service", facts) == ("unhealthy", ["TLS certificate has expired"])


# ------------------------------------------------------------------ relationships


def _node(rtype: str, ext: str, ips: list[str], facts: dict[str, Any] | None = None) -> Node:
    return Node(uuid.uuid4(), rtype, ext, ips, facts or {})


def test_derive_relationships() -> None:
    app_host, _ = parse_linux(LINUX_OUTPUT, "10.0.0.11")
    app = _node("linux_server", app_host.external_id, ["10.0.0.11"], app_host.facts)
    db = _node("linux_server", "host:db", ["10.0.0.12"])
    client = _node("windows_server", "host:client", ["10.0.0.50"])
    web = _node(
        "docker_container",
        f"container:{app_host.external_id}:web",
        [],
        {"container": container_facts(CONTAINERS[0])},
    )
    site = _node(
        "http_service",
        "http:http://10.0.0.11:8080/",
        ["10.0.0.11"],
        {"http": {"port": 8080, "resolved_ips": ["10.0.0.11"]}},
    )
    nodes = [app, db, client, web, site]
    edges = {(e.source_id, e.target_id, e.relationship_type): e for e in derive(nodes, nodes)}
    assert (web.id, app.id, "RUNS_ON") in edges
    connects = edges[(app.id, db.id, "CONNECTS_TO")]
    assert connects.properties["ports"] == [5432] and connects.derived_from == app.id
    assert (client.id, app.id, "CONNECTS_TO") in edges  # inbound from an inventoried client
    # The URL's port is published by the web container -> it runs on the container.
    assert (site.id, web.id, "RUNS_ON") in edges
    # Unknown peers (10.0.0.99) and loopback produce nothing.
    assert len(edges) == 4


# ------------------------------------------------------------------ SSH transport


async def test_ssh_collect_pins_and_refuses_changed_key() -> None:
    host = FakeHost()
    key = asyncssh.generate_private_key("ssh-ed25519")
    server = await start_ssh_server(host, key)
    port = port_of(server)
    creds = {"username": "ops", "password": "s3cret"}
    try:
        collector = LinuxSSHCollector({"port": port}, creds, pins={})
        result = await collector.collect(Target("127.0.0.1", name="app"))
        assert result.ok, result.error
        assert result.host_key is not None
        pinned = result.host_key.public_key
        assert result.host_key.fingerprint == key.get_fingerprint("sha256")
        names = [r.name for r in result.resources]
        assert names == ["app", "web", "migrate"]
        assert result.resources[0].facts["docker"]["accessible"] is True
        assert result.resources[0].facts["docker"]["server_version"] == "27.3.1"
        assert host.commands[0] == "sh -s"

        # Pinned key matches: works.
        pins = {("127.0.0.1", port): pinned}
        again = await LinuxSSHCollector({"port": port}, creds, pins).collect(Target("127.0.0.1"))
        assert again.ok

        # Wrong password.
        bad = LinuxSSHCollector({"port": port}, {**creds, "password": "nope"}, pins)
        failed = await bad.collect(Target("127.0.0.1"))
        assert not failed.ok and failed.error_code == "auth_failed"
    finally:
        server.close()

    # Same address, new host key: refused before anything runs.
    other_key = asyncssh.generate_private_key("ssh-ed25519")
    host2 = FakeHost()
    server2 = await start_ssh_server(host2, other_key)
    try:
        pins = {("127.0.0.1", port_of(server2)): pinned}
        collector = LinuxSSHCollector({"port": port_of(server2)}, creds, pins)
        refused = await collector.collect(Target("127.0.0.1"))
        assert not refused.ok and refused.error_code == "host_key_changed"
        assert refused.details["presented_fingerprint"] == other_key.get_fingerprint("sha256")
        assert host2.commands == []
    finally:
        server2.close()


async def test_ssh_docker_permission_denied_and_test() -> None:
    host = FakeHost(docker_info=None)
    key = asyncssh.generate_private_key("ssh-ed25519")
    server = await start_ssh_server(host, key)
    try:
        collector = LinuxSSHCollector(
            {"port": port_of(server)}, {"username": "ops", "password": "s3cret"}, {}
        )
        result = await collector.collect(Target("127.0.0.1"))
        assert result.ok and len(result.resources) == 1
        assert result.resources[0].facts["docker"]["status"] == "permission denied"
        outcome = await collector.test(Target("127.0.0.1"))
        assert outcome.ok and "Connected as ops" in outcome.message
        assert outcome.details["docker"].startswith("permission denied")
        assert outcome.host_key is not None
    finally:
        server.close()


async def test_ssh_unreachable_and_missing_credentials() -> None:
    collector = LinuxSSHCollector(
        {"port": 1, "timeout_seconds": 3}, {"username": "u", "password": "p"}, {}
    )
    result = await collector.collect(Target("127.0.0.1"))
    assert not result.ok and result.error_code == "unreachable"
    missing = await LinuxSSHCollector({}, {"username": "u"}, {}).collect(Target("127.0.0.1"))
    assert not missing.ok and missing.error_code == "missing_credentials"
