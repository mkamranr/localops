"""Discovery agent: inventory-backed entity resolution and evidence (no LLM).

Everything returned here comes from the stored inventory, so it is factual and
cites its source and observation time. It never touches credentials.
"""

from __future__ import annotations

import ipaddress
import uuid
from typing import Any

from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Resource, ResourceRelationship
from app.services.resource_service import blast_radius

MAX_CANDIDATES = 3


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _summary(r: Resource) -> dict[str, Any]:
    return {
        "name": r.name,
        "resource_id": str(r.id),
        "resource_type": r.resource_type,
        "health": r.health_status,
        "environment": r.environment,
        "stale": r.stale,
    }


async def resolve_entities(session: AsyncSession, entities: list[str]) -> list[dict[str, Any]]:
    """Match entity names from the request against the inventory.

    Exact name / hostname / IP matches resolve. A single partial match also resolves
    (marked ``partial``); several partial matches are reported as candidates only.
    """
    resolved: list[dict[str, Any]] = []
    for entity in entities:
        key = entity.strip()
        if not key:
            continue
        lowered = key.lower()
        conditions = [
            func.lower(Resource.name) == lowered,
            func.lower(Resource.hostname) == lowered,
            func.split_part(func.lower(Resource.hostname), ".", 1) == lowered,
        ]
        if _is_ip(key):
            conditions.append(Resource.ip_addresses.contains([key]))
        exact = list(await session.scalars(select(Resource).where(or_(*conditions)).limit(5)))
        if len(exact) == 1 or (exact and _is_ip(key)):
            resolved.append(
                {"entity": key, "resolved": True, "match": "exact", **_summary(exact[0])}
            )
            continue
        if len(exact) > 1:
            resolved.append(
                {
                    "entity": key,
                    "name": key,
                    "resolved": False,
                    "resource_id": None,
                    "reason": "ambiguous",
                    "candidates": [_summary(r) for r in exact[:MAX_CANDIDATES]],
                }
            )
            continue
        pattern = "%" + lowered.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        partial = list(
            await session.scalars(
                select(Resource)
                .where(
                    or_(
                        func.lower(Resource.name).like(pattern, escape="\\"),
                        func.lower(Resource.hostname).like(pattern, escape="\\"),
                        cast(Resource.tags, Text).ilike(pattern, escape="\\"),
                    )
                )
                .limit(MAX_CANDIDATES + 1)
            )
        )
        if len(partial) == 1 and len(lowered) >= 3:
            resolved.append(
                {"entity": key, "resolved": True, "match": "partial", **_summary(partial[0])}
            )
        else:
            resolved.append(
                {
                    "entity": key,
                    "name": key,
                    "resolved": False,
                    "resource_id": None,
                    "reason": "ambiguous" if partial else "not_in_inventory",
                    "candidates": [_summary(r) for r in partial[:MAX_CANDIDATES]],
                }
            )
    return resolved


def _host_findings(facts: dict[str, Any]) -> list[str]:
    out = []
    for d in facts.get("disks") or []:
        if (d.get("used_percent") or 0) >= 80:
            out.append(f"disk {d.get('mount')} {d.get('used_percent'):.0f}% used")
    mem = (facts.get("memory") or {}).get("used_percent")
    if mem is not None:
        out.append(f"memory {mem:.0f}% used")
    load, cpus = facts.get("load") or [], facts.get("cpu_count")
    if load and cpus:
        out.append(f"load {'/'.join(f'{v:.2f}' for v in load)} on {cpus} CPUs")
    if facts.get("failed_services"):
        out.append("failed services: " + ", ".join(facts["failed_services"][:10]))
    docker = facts.get("docker") or {}
    if docker.get("accessible"):
        out.append(f"docker: {docker.get('running')}/{docker.get('containers')} containers running")
    return out


def _container_findings(c: dict[str, Any]) -> list[str]:
    out = [f"state {c.get('state')}" + (f" (health {c['health']})" if c.get("health") else "")]
    if c.get("image"):
        out.append(f"image {c['image']}")
    if c.get("restart_count"):
        out.append(f"restarted {c['restart_count']} times")
    if c.get("state") == "exited":
        out.append(f"exit code {c.get('exit_code')}")
    return out


def _http_findings(h: dict[str, Any]) -> list[str]:
    if h.get("error"):
        return [f"request failed: {h['error']}"]
    out = [f"HTTP {h.get('status_code')} in {h.get('latency_ms')} ms"]
    tls = h.get("tls") or {}
    if tls.get("days_left") is not None:
        out.append(f"TLS certificate expires in {tls['days_left']} days")
    return out


async def evidence_for(
    session: AsyncSession, resource_ids: list[uuid.UUID]
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for rid in resource_ids:
        r = await session.get(Resource, rid)
        if r is None:
            continue
        facts = r.facts or {}
        if r.resource_type == "docker_container":
            findings = _container_findings(facts.get("container") or {})
        elif r.resource_type == "http_service":
            findings = _http_findings(facts.get("http") or {})
        else:
            findings = _host_findings(facts)
        upstream = list(
            await session.scalars(
                select(Resource.name)
                .join(ResourceRelationship, ResourceRelationship.target_id == Resource.id)
                .where(ResourceRelationship.source_id == rid)
                .limit(10)
            )
        )
        dependents = await blast_radius(session, rid)
        status = f"Health {r.health_status}"
        if r.health_reasons:
            status += ": " + "; ".join(r.health_reasons)
        if r.stale:
            status += " (stale: not found in the latest discovery)"
        evidence.append(
            {
                "source": "inventory",
                "agent": "discovery",
                "resource_id": str(r.id),
                "resource": r.name,
                "resource_type": r.resource_type,
                "summary": status,
                "findings": findings,
                "depends_on": upstream,
                "dependents": len(dependents),
                "health": r.health_status,
                "observed_at": (r.last_health_at or r.last_seen_at or r.created_at).isoformat(),
            }
        )
    return evidence
