"""Derive relationships from collected facts (pure functions; no database access).

* container RUNS_ON its Docker host
* host CONNECTS_TO an inventoried resource, from established TCP connections: the
  remote IP must belong to a known resource; if the remote port is a container's
  published port on that host, the edge points at the container instead
* an inventoried client CONNECTS_TO a collected host (inbound connections)
* http_service RUNS_ON the host (or container) its URL resolves to

Edges carry ``derived_from`` (the collected resource whose facts produced them) so
the next discovery of that resource can replace them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

HOST_TYPES = ("linux_server", "windows_server", "docker_host")


@dataclass(slots=True)
class Node:
    id: uuid.UUID
    resource_type: str
    external_id: str | None
    ip_addresses: list[str]
    facts: dict[str, Any]


@dataclass(slots=True)
class Edge:
    source_id: uuid.UUID
    target_id: uuid.UUID
    relationship_type: str
    derived_from: uuid.UUID
    confidence: float
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[uuid.UUID, uuid.UUID, str]:
        return (self.source_id, self.target_id, self.relationship_type)


class _Index:
    def __init__(self, nodes: list[Node]) -> None:
        self.by_ext = {n.external_id: n for n in nodes if n.external_id}
        self.by_ip: dict[str, list[Node]] = {}
        for n in nodes:
            for ip in n.ip_addresses:
                self.by_ip.setdefault(ip, []).append(n)
        # host external id -> {published host port: container node}
        self.published: dict[str, dict[int, Node]] = {}
        for n in nodes:
            if n.resource_type != "docker_container" or not n.external_id:
                continue
            host_ext = n.external_id.removeprefix("container:").rsplit(":", 1)[0]
            for p in (n.facts.get("container") or {}).get("ports") or []:
                if p.get("host_port"):
                    self.published.setdefault(host_ext, {})[int(p["host_port"])] = n

    def host_for_ip(self, ip: str, exclude: uuid.UUID | None = None) -> Node | None:
        candidates = [n for n in self.by_ip.get(ip, []) if n.id != exclude]
        hosts = [n for n in candidates if n.resource_type in HOST_TYPES]
        pool = hosts or candidates
        return pool[0] if pool else None

    def endpoint(self, ip: str, port: int, exclude: uuid.UUID) -> tuple[Node, float] | None:
        host = self.host_for_ip(ip, exclude)
        if host is None:
            return None
        container = self.published.get(host.external_id or "", {}).get(port)
        if container is not None and container.id != exclude:
            return container, 0.9
        return host, 0.8


def derive(seen: list[Node], all_nodes: list[Node]) -> list[Edge]:
    index = _Index(all_nodes)
    edges: dict[tuple[uuid.UUID, uuid.UUID, str], Edge] = {}

    def add(edge: Edge, port: int | None = None) -> None:
        existing = edges.get(edge.key)
        if existing is None:
            edges[edge.key] = edge
            existing = edge
        if port is not None:
            ports = existing.properties.setdefault("ports", [])
            if port not in ports and len(ports) < 50:
                ports.append(port)
                ports.sort()

    for node in seen:
        if node.resource_type == "docker_container" and node.external_id:
            host_ext = node.external_id.removeprefix("container:").rsplit(":", 1)[0]
            host = index.by_ext.get(host_ext)
            if host is not None:
                add(Edge(node.id, host.id, "RUNS_ON", node.id, 1.0))
        conns = node.facts.get("connections") or {}
        for c in conns.get("outbound") or []:
            hit = index.endpoint(c["remote_ip"], int(c["remote_port"]), node.id)
            if hit:
                target, conf = hit
                add(Edge(node.id, target.id, "CONNECTS_TO", node.id, conf), int(c["remote_port"]))
        for c in conns.get("inbound") or []:
            client = index.host_for_ip(c["remote_ip"], node.id)
            if client is not None and client.resource_type != "http_service":
                add(Edge(client.id, node.id, "CONNECTS_TO", node.id, 0.7), int(c["local_port"]))
        if node.resource_type == "http_service":
            http = node.facts.get("http") or {}
            port = int(http.get("port") or 0)
            for ip in http.get("resolved_ips") or []:
                hit = index.endpoint(ip, port, node.id)
                if hit:
                    target, conf = hit
                    add(Edge(node.id, target.id, "RUNS_ON", node.id, conf - 0.1), port)
                    break
    return list(edges.values())
