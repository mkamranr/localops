"""Tools over LocalOps' own data: inventory facts, dependencies, metric history,
recent changes and similar past incidents. They contact no host."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from app.models import ResourceEvent
from app.services import knowledge_search
from app.services import resource_service as rs
from app.tools.base import ResourceArgs, StrictArgs, ToolOutput, ToolSpec
from app.tools.context import ToolContext

ANY = (
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

# Facts worth showing a model; bulky lists are trimmed.
_FACT_KEYS = (
    "os", "kernel", "uptime_seconds", "load", "cpu_count", "cpu_percent", "memory", "disks",
    "failed_services", "stopped_auto_services", "docker", "container", "http", "domain",
)  # fmt: skip


async def resource_facts(ctx: ToolContext, args: ResourceArgs) -> ToolOutput:
    r = ctx.require_resource()
    facts = {k: r.facts[k] for k in _FACT_KEYS if k in r.facts}
    services = r.facts.get("services") or []
    ports = r.facts.get("listening_ports") or []
    body = {
        "name": r.name,
        "type": r.resource_type,
        "hostname": r.hostname,
        "ip_addresses": r.ip_addresses,
        "health": r.health_status,
        "facts": facts,
        "running_services": [s.get("name") for s in services][:60],
        "listening_ports": [p.get("port") for p in ports][:60],
    }
    return ToolOutput(
        json.dumps(body, indent=1, default=str),
        f"Inventory facts for {r.name} (health {r.health_status}, from the last discovery)",
    )


class DependencyArgs(ResourceArgs):
    direction: Literal["up", "down", "both"] = "both"
    depth: int = Field(2, ge=1, le=4)


async def dependencies(ctx: ToolContext, args: DependencyArgs) -> ToolOutput:
    r = ctx.require_resource()
    async with ctx.db() as session:
        g = await rs.dependencies(session, r.id, args.direction, args.depth)
    names = {n.id: n for n in g.nodes}
    lines = [
        f"{names[e.source_id].name} {e.relationship_type} {names[e.target_id].name}"
        for e in g.edges
        if e.source_id in names and e.target_id in names
    ]
    up = [f"{n.name} ({n.health_status})" for n in g.nodes if n.direction == "upstream"]
    down = [f"{n.name} ({n.health_status})" for n in g.nodes if n.direction == "downstream"]
    text = (
        f"depends on: {', '.join(up) or '(none)'}\n"
        f"dependents (blast radius): {', '.join(down) or '(none)'}\n"
        "edges:\n" + "\n".join(lines)
    )
    unhealthy_up = [
        n.name
        for n in g.nodes
        if n.direction == "upstream" and n.health_status in ("unhealthy", "degraded")
    ]
    summary = f"{len(up)} upstream, {len(down)} downstream" + (
        f"; unhealthy dependencies: {', '.join(unhealthy_up)}" if unhealthy_up else ""
    )
    return ToolOutput(text, summary)


class WindowArgs(ResourceArgs):
    hours: int = Field(24, ge=1, le=168)


async def metric_history(ctx: ToolContext, args: WindowArgs) -> ToolOutput:
    r = ctx.require_resource()
    async with ctx.db() as session:
        samples = await rs.metrics(session, r.id, args.hours)
    if not samples:
        return ToolOutput("(no samples)", f"No metric samples for {r.name} in {args.hours} h")
    series: dict[str, list[float]] = {}
    for s in samples:
        for key in ("cpu_load", "memory_percent", "disk_percent", "latency_ms"):
            value = getattr(s, key)
            if value is not None:
                series.setdefault(key, []).append(float(value))
    first, last = samples[0].taken_at, samples[-1].taken_at
    lines = [f"{len(samples)} samples, {first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M} UTC"]
    parts = []
    for key, values in series.items():
        lines.append(
            f"{key}: first {values[0]:.1f}, last {values[-1]:.1f}, min {min(values):.1f}, "
            f"max {max(values):.1f}"
        )
        parts.append(f"{key} {values[0]:.1f}→{values[-1]:.1f}")
    return ToolOutput("\n".join(lines), f"Metric trend over {args.hours} h: {'; '.join(parts)}")


async def recent_changes(ctx: ToolContext, args: WindowArgs) -> ToolOutput:
    r = ctx.require_resource()
    since = datetime.now(UTC) - timedelta(hours=args.hours)
    async with ctx.db() as session:
        events = list(
            await session.scalars(
                select(ResourceEvent)
                .where(ResourceEvent.resource_id == r.id, ResourceEvent.created_at >= since)
                .order_by(ResourceEvent.created_at.desc())
                .limit(40)
            )
        )
    if not events:
        return ToolOutput("(none)", f"No recorded changes to {r.name} in the last {args.hours} h")
    lines = [f"{e.created_at:%Y-%m-%d %H:%M} {e.event_type}: {e.summary}" for e in events]
    return ToolOutput(
        "\n".join(lines), f"{len(events)} recorded changes to {r.name} in {args.hours} h"
    )


class SimilarArgs(StrictArgs):
    query: str = Field(
        description="Symptoms or keywords to search for", min_length=3, max_length=300
    )
    resource_id: str | None = Field(None, description="Optional resource to match on")


async def similar_incidents(ctx: ToolContext, args: SimilarArgs) -> ToolOutput:
    ids: list[uuid.UUID] = [ctx.resource.id] if ctx.resource else []
    async with ctx.db() as session:
        incidents = await knowledge_search.similar_incidents(session, args.query, ids)
        investigations = await knowledge_search.similar_investigations(
            session, args.query, ids, exclude_id=ctx.investigation_id
        )
    items = incidents + investigations
    if not items:
        return ToolOutput("(no matches)", "No similar past incidents or investigations")
    lines = []
    for i in items:
        label = str(i["number"]) if i["kind"] == "incident" else "investigation"
        cause = i.get("root_cause") or i.get("conclusion") or "no recorded cause"
        resolution = f"; resolution: {i['resolution']}" if i.get("resolution") else ""
        lines.append(
            f"{label} {i['created_at'][:10]} [{i['status']}] {i['title']} — {cause}{resolution}"
        )
    return ToolOutput(
        "\n".join(lines),
        f"{len(incidents)} similar incidents and {len(investigations)} past investigations",
        {"matches": items},
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "inventory_resource",
        "Facts LocalOps recorded for a resource at its last discovery: OS, CPU, memory, "
        "disks, failed services, container state, HTTP check results.",
        ResourceArgs,
        resource_facts,
        resource_types=ANY,
        remote=False,
        category="inventory",
    ),
    ToolSpec(
        "inventory_dependencies",
        "What a resource depends on and what depends on it, with their health.",
        DependencyArgs,
        dependencies,
        resource_types=ANY,
        remote=False,
        category="inventory",
    ),
    ToolSpec(
        "resource_metrics",
        "Trend of CPU load, memory, disk and latency samples recorded by discovery.",
        WindowArgs,
        metric_history,
        resource_types=ANY,
        remote=False,
        category="inventory",
    ),
    ToolSpec(
        "recent_changes",
        "Changes LocalOps recorded for a resource (packages, services, ports, health, "
        "container restarts, manual edits) in a time window.",
        WindowArgs,
        recent_changes,
        resource_types=ANY,
        remote=False,
        category="inventory",
    ),
    ToolSpec(
        "search_similar_incidents",
        "Search past incidents and investigations for similar symptoms, with their root "
        "causes and resolutions.",
        SimilarArgs,
        similar_incidents,
        remote=False,
        category="knowledge",
    ),
)
