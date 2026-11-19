"""Investigation endpoints, including the live event stream (SSE)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from sqlalchemy import select

from app.api.dependencies import (
    client_info,
    dispatcher_dep,
    redis_dep,
    require_permission,
    require_principal,
)
from app.core.logging import get_logger
from app.database.session import tenant_session
from app.graphs import events
from app.graphs.dispatch import InvestigationDispatcher
from app.models import TERMINAL_STATUSES, Investigation
from app.schemas.ai import (
    InvestigationCreate,
    InvestigationDetail,
    InvestigationEventOut,
    InvestigationOut,
    InvestigationResume,
    ReportBrief,
)
from app.schemas.common import Page, PageParams
from app.services import investigation_service as svc
from app.services.context import ClientInfo, Principal, RequestContext

router = APIRouter(prefix="/investigations", tags=["investigations"])
log = get_logger("localops.sse")

KEEPALIVE_SECONDS = 15.0

Status = Literal["queued", "running", "awaiting_input", "completed", "failed", "cancelled"]


@router.post("", response_model=InvestigationOut, status_code=202)
async def create_investigation(
    body: InvestigationCreate,
    client: ClientInfo = Depends(client_info),
    dispatcher: InvestigationDispatcher = Depends(dispatcher_dep),
    principal: Principal = Depends(require_principal("incident.investigate")),
) -> InvestigationOut:
    inv = await svc.create(principal, client, body, dispatcher)
    return InvestigationOut.model_validate(inv)


@router.get("", response_model=Page[InvestigationOut])
async def list_investigations(
    page: PageParams = Depends(),
    status: Status | None = None,
    active: bool | None = None,
    q: str | None = Query(None, max_length=200),
    ctx: RequestContext = Depends(require_permission("incident.read")),
) -> Page[InvestigationOut]:
    rows, total = await svc.list_investigations(ctx, page, status=status, q=q, active=active)
    return Page(
        items=[
            InvestigationOut.model_validate(r).model_copy(
                update={"report": ReportBrief.from_result(r.result)}
            )
            for r in rows
        ],
        total=total,
        page=page.page,
        page_size=page.page_size,
    )


@router.get("/{investigation_id}", response_model=InvestigationDetail)
async def get_investigation(
    investigation_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission("incident.read")),
) -> InvestigationDetail:
    return await svc.get(ctx, investigation_id)


@router.get("/{investigation_id}/events", response_model=list[InvestigationEventOut])
async def list_events(
    investigation_id: uuid.UUID,
    after: int = Query(0, ge=0),
    ctx: RequestContext = Depends(require_permission("incident.read")),
) -> list[InvestigationEventOut]:
    return await svc.list_events(ctx, investigation_id, after)


@router.post("/{investigation_id}/resume", response_model=InvestigationOut, status_code=202)
async def resume_investigation(
    investigation_id: uuid.UUID,
    body: InvestigationResume,
    client: ClientInfo = Depends(client_info),
    dispatcher: InvestigationDispatcher = Depends(dispatcher_dep),
    redis: Redis = Depends(redis_dep),
    principal: Principal = Depends(require_principal("incident.investigate")),
) -> InvestigationOut:
    inv = await svc.resume(principal, client, investigation_id, body.answer, dispatcher, redis)
    return InvestigationOut.model_validate(inv)


@router.post("/{investigation_id}/cancel", response_model=InvestigationOut)
async def cancel_investigation(
    investigation_id: uuid.UUID,
    client: ClientInfo = Depends(client_info),
    redis: Redis = Depends(redis_dep),
    principal: Principal = Depends(require_principal("incident.investigate")),
) -> InvestigationOut:
    return await svc.cancel(principal, client, investigation_id, redis)


def _sse(event: InvestigationEventOut) -> str:
    payload = event.model_dump(mode="json")
    return f"id: {event.seq}\nevent: {event.event_type}\ndata: {json.dumps(payload)}\n\n"


@router.get("/{investigation_id}/stream")
async def stream_events(
    investigation_id: uuid.UUID,
    request: Request,
    after: int = Query(0, ge=0),
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    redis: Redis = Depends(redis_dep),
    principal: Principal = Depends(require_principal("incident.read")),
) -> StreamingResponse:
    """Server-Sent Events: replays events after ``Last-Event-ID``/``after``, then follows.

    No database connection is held between polls; Redis notifications only wake the
    loop, and every event is read from PostgreSQL (RLS applies).
    """
    await svc.exists(principal, investigation_id)
    cursor = after
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))
    tenant_id = principal.tenant_id

    async def generate() -> AsyncIterator[str]:
        nonlocal cursor
        pubsub: PubSub | None = redis.pubsub()
        try:
            await pubsub.subscribe(events.channel(tenant_id, investigation_id))  # type: ignore[union-attr]
        except Exception:  # Redis down: degrade to polling
            pubsub = None
        yield "retry: 3000\n\n"
        try:
            while True:
                async with tenant_session(tenant_id) as s:
                    rows = await events.list_events(s, investigation_id, after=cursor)
                    status = await s.scalar(
                        select(Investigation.status).where(Investigation.id == investigation_id)
                    )
                    batch = [InvestigationEventOut.model_validate(r) for r in rows]
                for event in batch:
                    cursor = event.seq
                    yield _sse(event)
                if status is None or (status in TERMINAL_STATUSES and len(batch) < 500):
                    yield f"event: end\ndata: {json.dumps({'status': status})}\n\n"
                    return
                if await request.is_disconnected():
                    return
                woke = await _wait(pubsub, KEEPALIVE_SECONDS)
                if not woke:
                    yield ": keepalive\n\n"
        finally:
            if pubsub is not None:
                with contextlib.suppress(Exception):
                    await pubsub.unsubscribe()
                    await pubsub.aclose()  # type: ignore[no-untyped-call]

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _wait(pubsub: PubSub | None, wait_seconds: float) -> bool:
    """Wait for a notification; returns False on keepalive timeout."""
    if pubsub is None:
        await asyncio.sleep(min(wait_seconds, 2.0))
        return True
    try:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=wait_seconds)
    except Exception:
        await asyncio.sleep(1.0)
        return True
    return message is not None
