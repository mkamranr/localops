"""Executes investigation graphs: start, resume after clarification, and recovery.

The runner is the only code that moves an investigation between statuses while it
executes. It is idempotent: running it again for the same investigation continues
from the last checkpoint, or does nothing if the investigation already finished. A
Redis lock prevents two workers from executing the same investigation at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.redis import tenant_key
from app.database.session import tenant_session
from app.graphs import events
from app.graphs.checkpointer import tenant_checkpointer, thread_id
from app.graphs.investigation import build_graph
from app.graphs.runtime import (
    AgentUnavailableError,
    ClientFactory,
    InvestigationCancelledError,
    InvestigationRuntime,
)
from app.llm.gateway import ModelClient, ModelError
from app.models import TERMINAL_STATUSES, Investigation

# Air-gapped: never let LangChain/LangGraph attempt to reach LangSmith.
os.environ.setdefault("LANGSMITH_TRACING", "false")
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")

log = get_logger("localops.runner")

LOCK_TTL = 120


class InvestigationLockedError(Exception):
    """Another worker currently runs this investigation."""


async def _acquire_lock(redis: Redis | None, key: str, token: str) -> bool:
    if redis is None:
        return True
    return bool(await redis.set(key, token, nx=True, ex=LOCK_TTL))


async def _release_lock(redis: Redis | None, key: str, token: str) -> None:
    if redis is None:
        return
    with contextlib.suppress(Exception):
        current = await redis.get(key)
        if (
            current is not None
            and (current.decode() if isinstance(current, bytes) else current) == token
        ):
            await redis.delete(key)


async def _keep_lock(redis: Redis, key: str) -> None:
    while True:
        await asyncio.sleep(LOCK_TTL / 3)
        with contextlib.suppress(Exception):
            await redis.expire(key, LOCK_TTL)


async def _transition(
    rt: InvestigationRuntime,
    *,
    event_type: str,
    data: dict[str, Any] | None = None,
    **fields: Any,
) -> None:
    """Update the investigation and append an event in one transaction."""
    async with tenant_session(rt.tenant_id) as session:
        inv = await session.get(Investigation, rt.investigation_id)
        if inv is None:
            return
        for name, value in fields.items():
            setattr(inv, name, value)
        await events.append_event(
            session,
            tenant_id=rt.tenant_id,
            investigation_id=rt.investigation_id,
            event_type=event_type,
            data=data,
        )
    await events.notify(rt.redis, rt.tenant_id, rt.investigation_id)


async def run_investigation(
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    investigation_id: uuid.UUID,
    redis: Redis | None,
    resume: str | None = None,
    client_factory: ClientFactory = ModelClient,
) -> str | None:
    """Run (or continue) an investigation. Returns the resulting status."""
    lock_key = tenant_key(tenant_id, "investigation", str(investigation_id), "lock")
    token = secrets.token_hex(8)
    if not await _acquire_lock(redis, lock_key, token):
        raise InvestigationLockedError(str(investigation_id))
    keeper = asyncio.create_task(_keep_lock(redis, lock_key)) if redis is not None else None
    rt = InvestigationRuntime(
        settings=settings,
        tenant_id=tenant_id,
        investigation_id=investigation_id,
        redis=redis,
        client_factory=client_factory,
    )
    try:
        return await _run(rt, settings, resume)
    finally:
        if keeper is not None:
            keeper.cancel()
        await _release_lock(redis, lock_key, token)


async def _run(rt: InvestigationRuntime, settings: Settings, resume: str | None) -> str | None:
    async with tenant_session(rt.tenant_id) as session:
        inv = await session.get(Investigation, rt.investigation_id)
        if inv is None or inv.status in TERMINAL_STATUSES:
            return inv.status if inv else None
        if inv.status == "awaiting_input" and resume is None:
            return inv.status
        cancel_requested = inv.cancel_requested
        request, mode, attempts = inv.request, inv.execution_mode, inv.attempts

    if cancel_requested:
        await _cancelled(rt)
        return "cancelled"

    await _transition(
        rt,
        event_type="investigation.resumed" if attempts else "investigation.started",
        data={"attempt": attempts + 1, "workflow": "incident_investigation.v1"},
        status="running",
        attempts=attempts + 1,
        pending_question=None,
    )

    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id(rt.tenant_id, rt.investigation_id)},
        "recursion_limit": 60,
    }
    try:
        async with tenant_checkpointer(settings, rt.tenant_id) as saver:
            graph = build_graph(
                rt, saver, max_clarifications=settings.investigation_max_clarifications
            )
            snapshot = await graph.aget_state(config)
            graph_input: Any
            if resume is not None:
                graph_input = Command(resume=resume)
            elif snapshot.next:
                if snapshot.interrupts:  # still waiting for an answer we don't have
                    return await _awaiting(rt, snapshot.interrupts[0].value)
                graph_input = None  # continue after a crash/restart from the checkpoint
            elif snapshot.values.get("report"):
                graph_input = None  # finished, but the status update was lost
            else:
                graph_input = {"request": request, "execution_mode": mode}

            if graph_input is not None or snapshot.next:
                async with asyncio.timeout(settings.investigation_timeout_seconds):
                    # "sync" persists each checkpoint before the next step starts, so a
                    # crash never loses a completed node.
                    await graph.ainvoke(graph_input, config, durability="sync")
            snapshot = await graph.aget_state(config)

        if snapshot.interrupts:
            return await _awaiting(rt, snapshot.interrupts[0].value)
        values = snapshot.values
        report = values.get("report") or {}
        await _transition(
            rt,
            event_type="investigation.completed",
            data={"conclusion": report.get("conclusion")},
            status="completed",
            result=report,
            request_type=(values.get("classification") or {}).get("request_type"),
            investigation_type=(values.get("classification") or {}).get("request_type"),
            resource_ids=[],
            completed_at=datetime.now(UTC),
        )
        return "completed"
    except InvestigationCancelledError:
        await _cancelled(rt)
        return "cancelled"
    except TimeoutError:
        return await _failed(rt, "Investigation exceeded its time limit")
    except (AgentUnavailableError, ModelError, AppError) as exc:
        detail = exc.detail if isinstance(exc, ModelError) else None
        return await _failed(rt, str(exc), detail=detail)
    except Exception as exc:
        log.exception("investigation_crashed", investigation_id=str(rt.investigation_id))
        return await _failed(rt, f"Internal error ({type(exc).__name__})")


async def _awaiting(rt: InvestigationRuntime, value: Any) -> str:
    question = value.get("question") if isinstance(value, dict) else str(value)
    await _transition(
        rt,
        event_type="clarification.requested",
        data={"question": question},
        status="awaiting_input",
        pending_question=question,
    )
    return "awaiting_input"


async def _cancelled(rt: InvestigationRuntime) -> None:
    await _transition(
        rt,
        event_type="investigation.cancelled",
        status="cancelled",
        pending_question=None,
        completed_at=datetime.now(UTC),
    )


async def _failed(rt: InvestigationRuntime, message: str, *, detail: str | None = None) -> str:
    # The model-output excerpt is shown to operators but kept out of the logs.
    log.warning("investigation_failed", investigation_id=str(rt.investigation_id), error=message)
    full = f"{message}\nModel output began: {detail}" if detail else message
    await _transition(
        rt,
        event_type="investigation.failed",
        data={"error": full[:900]},
        status="failed",
        error=full[:1000],
        completed_at=datetime.now(UTC),
    )
    return "failed"
