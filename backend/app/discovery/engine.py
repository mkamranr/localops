"""Discovery runs: collect from an integration's targets and update the inventory.

Three phases, so no database connection is held while talking to remote hosts:

1. load the integration, decrypt credentials and host-key pins, mark the run running;
2. collect from all targets concurrently (bounded);
3. apply: pin new host keys, upsert resources, record changes, health and metric
   samples, mark missing resources stale, refresh derived relationships, finish the
   run and audit it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import decrypt_secret
from app.core.logging import get_logger
from app.core.redis import tenant_key
from app.database.session import tenant_session
from app.discovery import health as health_rules
from app.discovery.relationships import Edge, Node, derive
from app.integrations import CollectorFactory, get_collector_factory, uses_ssh
from app.integrations.base import (
    CollectorError,
    DiscoveredResource,
    HostKeyObservation,
    Target,
    TargetResult,
)
from app.models import (
    DiscoveryRun,
    Integration,
    Resource,
    ResourceEvent,
    ResourceMetricSample,
    ResourceRelationship,
    SshHostKey,
)
from app.services import audit_service as audit
from app.services import incident_service
from app.services.incident_service import HealthChange

log = get_logger("localops.discovery")

LOCK_TTL = 600
# Connections are point-in-time; keep a derived CONNECTS_TO edge for a while after
# the connection was last observed instead of flapping on every run.
CONNECTION_GRACE = timedelta(hours=24)
HOST_TYPES = ("linux_server", "windows_server", "docker_host")


def _now() -> datetime:
    return datetime.now(UTC)


def load_credentials(settings: Settings, integration: Integration) -> dict[str, Any]:
    """Decrypted secrets plus the (non-secret) username from the configuration."""
    data: dict[str, Any] = {}
    if integration.encrypted_credentials:
        data = json.loads(decrypt_secret(settings, integration.encrypted_credentials))
    if integration.configuration.get("username"):
        data["username"] = integration.configuration["username"]
    return data


def ssh_port(integration: Integration, target: Target) -> int:
    return target.port or int(integration.configuration.get("port") or 22)


async def load_pins(
    session: AsyncSession, integration: Integration, targets: list[Target]
) -> dict[tuple[str, int], str]:
    if not uses_ssh(integration.integration_type, integration.configuration):
        return {}
    hosts = {t.host for t in targets}
    rows = await session.scalars(select(SshHostKey).where(SshHostKey.host.in_(hosts)))
    return {(r.host, r.port): r.public_key for r in rows}


async def record_host_key(
    session: AsyncSession, tenant_id: uuid.UUID, key: HostKeyObservation
) -> bool:
    """Pin a first-seen key, or refresh ``last_seen_at``. Returns True if newly pinned."""
    existing = await session.scalar(
        select(SshHostKey).where(SshHostKey.host == key.host, SshHostKey.port == key.port)
    )
    if existing is None:
        session.add(
            SshHostKey(
                tenant_id=tenant_id,
                host=key.host,
                port=key.port,
                key_type=key.key_type,
                public_key=key.public_key,
                fingerprint=key.fingerprint,
                last_seen_at=_now(),
            )
        )
        await session.flush()
        return True
    if existing.public_key == key.public_key:
        existing.last_seen_at = _now()
    return False


def targets_of(integration: Integration, settings: Settings) -> list[Target]:
    out = []
    for raw in integration.targets[: settings.discovery_max_targets]:
        with contextlib.suppress(KeyError, ValueError, TypeError):
            out.append(Target.from_dict(raw))
    return out


# --------------------------------------------------------------------------- locking


async def _acquire(redis: Redis | None, key: str, token: str) -> bool:
    if redis is None:
        return True
    return bool(await redis.set(key, token, nx=True, ex=LOCK_TTL))


async def _release(redis: Redis | None, key: str, token: str) -> None:
    if redis is None:
        return
    with contextlib.suppress(Exception):
        if await redis.get(key) == token:
            await redis.delete(key)


async def _keepalive(redis: Redis, key: str) -> None:
    while True:
        await asyncio.sleep(LOCK_TTL / 3)
        with contextlib.suppress(Exception):
            await redis.expire(key, LOCK_TTL)


# --------------------------------------------------------------------------- run


async def run_discovery(
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    redis: Redis | None = None,
    collector_factory: CollectorFactory | None = None,
) -> str:
    factory = collector_factory or get_collector_factory()
    async with tenant_session(tenant_id) as s:
        run = await s.get(DiscoveryRun, run_id)
        if run is None or run.status not in ("queued", "running"):
            return run.status if run else "missing"
        integration_id = run.integration_id

    lock_key = tenant_key(tenant_id, "discovery", str(integration_id))
    token = secrets.token_hex(8)
    if not await _acquire(redis, lock_key, token):
        await _fail_run(tenant_id, run_id, "Another discovery of this integration is running")
        return "failed"
    keeper = asyncio.create_task(_keepalive(redis, lock_key)) if redis is not None else None
    try:
        return await _run_locked(settings, tenant_id, run_id, factory)
    except Exception as exc:
        log.exception("discovery_failed", run_id=str(run_id))
        await _fail_run(tenant_id, run_id, f"Internal error ({type(exc).__name__})")
        return "failed"
    finally:
        if keeper is not None:
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper
        await _release(redis, lock_key, token)


async def _fail_run(tenant_id: uuid.UUID, run_id: uuid.UUID, message: str) -> None:
    async with tenant_session(tenant_id) as s:
        run = await s.get(DiscoveryRun, run_id)
        if run is None:
            return
        run.status = "failed"
        run.error = message[:1000]
        run.completed_at = _now()
        integration = await s.get(Integration, run.integration_id)
        if integration is not None:
            integration.last_run_at = run.completed_at
            integration.last_status = "failed"
            integration.last_error = message[:1000]


async def _run_locked(
    settings: Settings, tenant_id: uuid.UUID, run_id: uuid.UUID, factory: CollectorFactory
) -> str:
    # ---- phase 1: load
    async with tenant_session(tenant_id) as s:
        run = await s.get(DiscoveryRun, run_id)
        assert run is not None  # noqa: S101 - checked by the caller
        integration = await s.get(Integration, run.integration_id)
        if integration is None or not integration.enabled:
            run.status, run.error, run.completed_at = "failed", "Integration is disabled", _now()
            return "failed"
        targets = targets_of(integration, settings)
        only = (run.stats or {}).get("only_target")
        if only:  # single-resource health check
            targets = [t for t in targets if t.label == only]
        try:
            credentials = load_credentials(settings, integration)
        except Exception as exc:  # undecryptable credentials
            run.status, run.error, run.completed_at = "failed", str(exc)[:1000], _now()
            return "failed"
        pins = await load_pins(s, integration, targets)
        run.status = "running"
        integration_type = integration.integration_type
        config = dict(integration.configuration)

    # ---- phase 2: collect
    try:
        collector = factory(integration_type, config, credentials, pins)
    except CollectorError as exc:
        await _fail_run(tenant_id, run_id, str(exc))
        return "failed"
    semaphore = asyncio.Semaphore(settings.discovery_concurrency)

    async def one(target: Target) -> TargetResult:
        async with semaphore:
            try:
                return await collector.collect(target)
            except CollectorError as exc:
                return TargetResult(target, False, error=str(exc), error_code=exc.code)
            except Exception as exc:  # a collector bug must not sink the whole run
                log.warning("collector_crashed", target=target.label, error=type(exc).__name__)
                return TargetResult(target, False, error=f"Collector error ({type(exc).__name__})")

    results = list(await asyncio.gather(*(one(t) for t in targets)))

    # ---- phase 3: apply
    async with tenant_session(tenant_id) as s:
        return await apply_results(
            s,
            tenant_id,
            run_id,
            results,
            retention_days=settings.metric_retention_days,
            auto_incidents=settings.incident_auto_create,
        )


async def apply_results(
    s: AsyncSession,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    results: list[TargetResult],
    *,
    retention_days: int = 30,
    auto_incidents: bool = True,
) -> str:
    run = await s.get(DiscoveryRun, run_id)
    assert run is not None  # noqa: S101
    integration = await s.get(Integration, run.integration_id)
    assert integration is not None  # noqa: S101
    now = _now()
    stats: dict[str, Any] = {
        "targets": len(results),
        "succeeded": 0,
        "failed": 0,
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "stale": 0,
        "relationships_added": 0,
        "relationships_removed": 0,
    }
    target_rows: list[dict[str, Any]] = []
    seen: dict[uuid.UUID, DiscoveredResource] = {}
    ok_labels: set[str] = set()
    health_changes: list[HealthChange] = []

    for result in results:
        label = result.target.label
        row: dict[str, Any] = {
            "target": label,
            "ok": result.ok,
            "duration_ms": result.duration_ms,
            "resources": len(result.resources),
        }
        if result.host_key is not None:
            pinned = await record_host_key(s, tenant_id, result.host_key)
            row["host_key_fingerprint"] = result.host_key.fingerprint
            row["host_key_pinned_now"] = pinned
        if not result.ok:
            stats["failed"] += 1
            row["error"] = (result.error or "")[:500]
            row["error_code"] = result.error_code
            if result.details:
                row["details"] = result.details
            if result.error_code == "host_key_changed":
                await audit.record(
                    s,
                    tenant_id=tenant_id,
                    action="integration.host_key_mismatch",
                    outcome="failure",
                    username="system",
                    entity_type="integration",
                    entity_id=integration.id,
                    details={"target": label, **result.details},
                )
            await _mark_unreachable(
                s, integration, label, result.error or "unreachable", now, health_changes
            )
            target_rows.append(row)
            continue
        stats["succeeded"] += 1
        ok_labels.add(label)
        for discovered in result.resources:
            resource, outcome = await _upsert(
                s, tenant_id, integration, label, discovered, now, health_changes
            )
            stats[outcome] += 1
            seen[resource.id] = discovered
        target_rows.append(row)

    if (run.stats or {}).get("only_target"):
        stats["only_target"] = run.stats["only_target"]
    gone = await _mark_stale(s, integration, ok_labels, set(seen), now)
    stats["stale"] = len(gone)
    added, removed = await _refresh_relationships(s, tenant_id, seen, gone, now)
    if seen:  # metric retention for the resources this run touched
        await s.execute(
            delete(ResourceMetricSample).where(
                ResourceMetricSample.resource_id.in_(list(seen)),
                ResourceMetricSample.taken_at < now - timedelta(days=retention_days),
            )
        )
    stats["relationships_added"], stats["relationships_removed"] = added, removed
    if health_changes:
        opened = await incident_service.on_health_changes(
            s, tenant_id, health_changes, create=auto_incidents
        )
        if opened:
            stats["incidents_opened"] = opened

    status = (
        "completed" if stats["failed"] == 0 else ("partial" if stats["succeeded"] else "failed")
    )
    first_error = next((r.get("error") for r in target_rows if not r["ok"]), None)
    run.status = status
    run.completed_at = now
    run.stats = stats
    run.target_results = target_rows
    run.error = first_error if status == "failed" else None
    integration.last_run_at = now
    integration.last_status = status
    integration.last_error = first_error
    await audit.record(
        s,
        tenant_id=tenant_id,
        action="integration.discover",
        outcome="success" if status != "failed" else "failure",
        user_id=run.triggered_by,
        username=None if run.triggered_by else "system",
        entity_type="integration",
        entity_id=integration.id,
        details={"name": integration.name, "trigger": run.trigger, "status": status, **stats},
    )
    log.info("discovery_completed", run_id=str(run_id), status=status, **stats)
    return status


# --------------------------------------------------------------------------- upsert


def _event(
    resource: Resource, event_type: str, summary: str, details: dict[str, Any] | None = None
) -> ResourceEvent:
    return ResourceEvent(
        tenant_id=resource.tenant_id,
        resource_id=resource.id,
        event_type=event_type,
        summary=summary[:500],
        details=details or {},
        actor="discovery",
    )


async def _upsert(
    s: AsyncSession,
    tenant_id: uuid.UUID,
    integration: Integration,
    label: str,
    d: DiscoveredResource,
    now: datetime,
    health_changes: list[HealthChange],
) -> tuple[Resource, str]:
    status, reasons = health_rules.evaluate(d.resource_type, d.facts)
    resource = await s.scalar(select(Resource).where(Resource.external_id == d.external_id))
    if resource is None:
        resource = Resource(
            tenant_id=tenant_id,
            name=d.name[:200],
            resource_type=d.resource_type,
            hostname=(d.hostname or None) and d.hostname[:255],
            ip_addresses=d.ip_addresses,
            os=(d.os or None) and d.os[:200],
            facts=d.facts,
            health_status=status,
            health_reasons=reasons,
            last_health_at=now,
            source="discovered",
            integration_id=integration.id,
            external_id=d.external_id,
            discovery_target=label,
            last_seen_at=now,
            stale=False,
        )
        s.add(resource)
        await s.flush()
        health_changes.append(HealthChange(resource, None, status, reasons))
        s.add(
            _event(
                resource,
                "discovered",
                f"Discovered by {integration.name} ({label})",
                {"health": status, "reasons": reasons},
            )
        )
        _sample(s, resource, d, now)
        return resource, "added"

    changes = diff_resource(resource, d)
    outcome = "updated" if changes else "unchanged"
    if resource.stale:
        s.add(_event(resource, "restored", f"Seen again by {integration.name} ({label})"))
    if changes:
        s.add(_event(resource, "changed", change_summary(changes), {"changes": changes}))
    if status != resource.health_status:
        health_changes.append(HealthChange(resource, resource.health_status, status, reasons))
        s.add(
            _event(
                resource,
                "health_changed",
                f"Health {resource.health_status} → {status}",
                {"from": resource.health_status, "to": status, "reasons": reasons},
            )
        )
    # Keep the more specific type when a Docker-over-SSH run sees a Linux host.
    if not (resource.resource_type in HOST_TYPES and d.resource_type == "docker_host"):
        resource.resource_type = d.resource_type
    resource.hostname = (d.hostname or None) and d.hostname[:255]
    resource.ip_addresses = d.ip_addresses
    resource.os = (d.os or None) and d.os[:200]
    resource.facts = d.facts
    resource.health_status = status
    resource.health_reasons = reasons
    resource.last_health_at = now
    resource.integration_id = integration.id
    resource.discovery_target = label
    resource.last_seen_at = now
    resource.stale = False
    if resource.source == "manual":
        resource.source = "discovered"
    _sample(s, resource, d, now)
    return resource, outcome


def _sample(s: AsyncSession, resource: Resource, d: DiscoveredResource, now: datetime) -> None:
    m = d.metrics or {}
    if not any(v is not None for v in m.values()):
        return
    cpus = m.get("cpu_count")
    s.add(
        ResourceMetricSample(
            tenant_id=resource.tenant_id,
            resource_id=resource.id,
            taken_at=now,
            cpu_load=m.get("cpu_load"),
            cpu_count=int(cpus) if cpus is not None else None,
            memory_percent=m.get("memory_percent"),
            disk_percent=m.get("disk_percent"),
            latency_ms=m.get("latency_ms"),
        )
    )


def _names(items: list[Any], key: str | None = None) -> set[str]:
    return {str(i.get(key) if key and isinstance(i, dict) else i) for i in items or []}


def diff_resource(r: Resource, d: DiscoveredResource) -> dict[str, Any]:
    """Operator-relevant differences between stored and newly collected facts."""
    changes: dict[str, Any] = {}

    def scalar(name: str, old: Any, new: Any) -> None:
        if old != new and new is not None:
            changes[name] = {"from": old, "to": new}

    def setdiff(name: str, old: set[str], new: set[str]) -> None:
        added, removed = sorted(new - old), sorted(old - new)
        if added or removed:
            changes[name] = {"added": added[:50], "removed": removed[:50]}

    old, new = r.facts or {}, d.facts
    scalar("hostname", r.hostname, d.hostname)
    scalar("os", r.os, d.os)
    setdiff("ip_addresses", set(r.ip_addresses or []), set(d.ip_addresses))
    scalar("kernel", old.get("kernel"), new.get("kernel"))
    scalar("cpu_count", old.get("cpu_count"), new.get("cpu_count"))
    scalar(
        "memory_total_mb",
        (old.get("memory") or {}).get("total_mb"),
        (new.get("memory") or {}).get("total_mb"),
    )
    if "services" in new:
        setdiff(
            "services", _names(old.get("services", []), "name"), _names(new["services"], "name")
        )
    if "failed_services" in new:
        setdiff(
            "failed_services",
            _names(old.get("failed_services", [])),
            _names(new["failed_services"]),
        )
    if "listening_ports" in new:
        setdiff(
            "listening_ports",
            _names(old.get("listening_ports", []), "port"),
            _names(new["listening_ports"], "port"),
        )
    oc, nc = old.get("container") or {}, new.get("container") or {}
    if nc:
        scalar("image", oc.get("image"), nc.get("image"))
        scalar("state", oc.get("state"), nc.get("state"))
        scalar("container_id", (oc.get("id") or "")[:12] or None, (nc.get("id") or "")[:12])
    od, nd = old.get("docker") or {}, new.get("docker") or {}
    if nd.get("server_version"):
        scalar("docker_version", od.get("server_version"), nd.get("server_version"))
    oh, nh = old.get("http") or {}, new.get("http") or {}
    if nh:
        scalar("http_status", oh.get("status_code"), nh.get("status_code"))
    return changes


def change_summary(changes: dict[str, Any]) -> str:
    parts = []
    for name, change in changes.items():
        label = name.replace("_", " ")
        if "added" in change:
            parts.append(f"{label} (+{len(change['added'])}/-{len(change['removed'])})")
        else:
            parts.append(f"{label}: {change['from']} → {change['to']}")
    return "Changed " + "; ".join(parts)


# --------------------------------------------------------------------------- stale


async def _mark_unreachable(
    s: AsyncSession,
    integration: Integration,
    label: str,
    error: str,
    now: datetime,
    health_changes: list[HealthChange],
) -> None:
    rows = await s.scalars(
        select(Resource).where(
            Resource.integration_id == integration.id,
            Resource.discovery_target == label,
            Resource.source == "discovered",
        )
    )
    for r in rows:
        if r.resource_type in HOST_TYPES or r.resource_type == "http_service":
            status, reasons = "unhealthy", [f"Unreachable during discovery: {error[:200]}"]
        else:
            status, reasons = "unknown", ["Its host could not be collected"]
        if status != r.health_status:
            health_changes.append(HealthChange(r, r.health_status, status, reasons))
            s.add(
                _event(
                    r,
                    "health_changed",
                    f"Health {r.health_status} → {status}",
                    {"from": r.health_status, "to": status, "reasons": reasons},
                )
            )
        r.health_status, r.health_reasons, r.last_health_at = status, reasons, now


async def _mark_stale(
    s: AsyncSession,
    integration: Integration,
    ok_labels: set[str],
    seen_ids: set[uuid.UUID],
    now: datetime,
) -> set[uuid.UUID]:
    """Mark resources this integration no longer finds as stale; returns their ids."""
    current = {Target.from_dict(t).label for t in integration.targets if t.get("host")}
    rows = await s.scalars(
        select(Resource).where(
            Resource.integration_id == integration.id,
            Resource.source == "discovered",
            Resource.stale.is_(False),
        )
    )
    gone: set[uuid.UUID] = set()
    for r in rows:
        if r.id in seen_ids:
            continue
        # Not seen on a target that was collected fine, or its target was removed.
        if r.discovery_target in ok_labels or r.discovery_target not in current:
            r.stale = True
            r.health_status, r.health_reasons = "unknown", ["Not found in the latest discovery"]
            r.last_health_at = now
            s.add(_event(r, "stale", f"Not found by {integration.name}; marked stale"))
            gone.add(r.id)
    return gone


# --------------------------------------------------------------------------- edges


async def _refresh_relationships(
    s: AsyncSession,
    tenant_id: uuid.UUID,
    seen: dict[uuid.UUID, DiscoveredResource],
    gone: set[uuid.UUID],
    now: datetime,
) -> tuple[int, int]:
    """Upsert edges derived from the resources seen in this run; drop edges whose
    deriving resource no longer yields them, or has gone stale."""
    if not seen and not gone:
        return 0, 0
    await s.flush()
    rows = (
        await s.execute(
            select(
                Resource.id,
                Resource.resource_type,
                Resource.external_id,
                Resource.ip_addresses,
                Resource.facts,
            ).where(Resource.stale.is_(False))
        )
    ).all()
    nodes = [Node(r[0], r[1], r[2], list(r[3] or []), r[4] or {}) for r in rows]
    by_id = {n.id: n for n in nodes}
    desired = derive([by_id[i] for i in seen if i in by_id], nodes)
    desired_keys = {e.key: e for e in desired}

    existing = list(await s.scalars(select(ResourceRelationship)))
    by_key = {(e.source_id, e.target_id, e.relationship_type): e for e in existing}
    seen_text = {str(i) for i in seen}
    gone_text = {str(i) for i in gone}
    added = removed = 0
    for key, edge in desired_keys.items():
        row = by_key.get(key)
        if row is not None and row.source == "manual":
            continue  # an operator already recorded this relationship
        if row is None:
            s.add(_edge_row(tenant_id, edge, now))
            added += 1
        else:
            row.properties = {**edge.properties, "derived_from": str(edge.derived_from)}
            row.confidence = edge.confidence
            row.last_seen_at = now
    stale_ids = []
    for key, row in by_key.items():
        if row.source != "discovered" or key in desired_keys:
            continue
        origin = (row.properties or {}).get("derived_from")
        if origin in gone_text:
            stale_ids.append(row.id)
            continue
        if origin not in seen_text:
            continue
        recent = row.last_seen_at and now - row.last_seen_at < CONNECTION_GRACE
        if row.relationship_type == "CONNECTS_TO" and recent:
            continue
        stale_ids.append(row.id)
    if stale_ids:
        await s.execute(delete(ResourceRelationship).where(ResourceRelationship.id.in_(stale_ids)))
        removed = len(stale_ids)
    return added, removed


def _edge_row(tenant_id: uuid.UUID, edge: Edge, now: datetime) -> ResourceRelationship:
    return ResourceRelationship(
        tenant_id=tenant_id,
        source_id=edge.source_id,
        target_id=edge.target_id,
        relationship_type=edge.relationship_type,
        source="discovered",
        confidence=edge.confidence,
        properties={**edge.properties, "derived_from": str(edge.derived_from)},
        last_seen_at=now,
    )
