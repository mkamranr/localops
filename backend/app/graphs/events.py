"""Investigation event log.

Events are the durable, ordered record of what happened during an investigation
(spec §11.3, §10.10). Each event gets a per-investigation sequence number assigned
under a row lock on the investigation, so the SSE stream can resume with
``Last-Event-ID`` without gaps or duplicates. After commit a tiny notification is
published on a tenant-namespaced Redis channel to wake live streams; the database
stays the source of truth.
"""

from __future__ import annotations

import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.redis import tenant_key
from app.models import Investigation, InvestigationEvent

log = get_logger("localops.events")


def channel(tenant_id: uuid.UUID, investigation_id: uuid.UUID) -> str:
    return tenant_key(tenant_id, "investigation", str(investigation_id), "events")


async def append_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    investigation_id: uuid.UUID,
    event_type: str,
    agent: str | None = None,
    data: dict[str, Any] | None = None,
) -> int:
    """Insert an event in the caller's transaction and return its sequence number.

    Takes ``FOR UPDATE`` on the investigation row; callers must not hold that lock in
    a *different* open transaction or they will deadlock.
    """
    await session.execute(
        select(Investigation.id).where(Investigation.id == investigation_id).with_for_update()
    )
    current = await session.scalar(
        select(func.coalesce(func.max(InvestigationEvent.seq), 0)).where(
            InvestigationEvent.investigation_id == investigation_id
        )
    )
    seq = int(current or 0) + 1
    session.add(
        InvestigationEvent(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            seq=seq,
            event_type=event_type,
            agent_name=agent,
            event_data=data or {},
        )
    )
    await session.flush()
    return seq


async def notify(redis: Redis | None, tenant_id: uuid.UUID, investigation_id: uuid.UUID) -> None:
    """Wake live streams. Best effort: streams also poll, so a lost notify only delays."""
    if redis is None:
        return
    try:
        await redis.publish(channel(tenant_id, investigation_id), "1")
    except Exception as exc:  # pragma: no cover - depends on Redis availability
        log.warning("event_notify_failed", error=type(exc).__name__)


async def list_events(
    session: AsyncSession, investigation_id: uuid.UUID, *, after: int = 0, limit: int = 500
) -> list[InvestigationEvent]:
    rows = await session.scalars(
        select(InvestigationEvent)
        .where(InvestigationEvent.investigation_id == investigation_id)
        .where(InvestigationEvent.seq > after)
        .order_by(InvestigationEvent.seq)
        .limit(limit)
    )
    return list(rows)
