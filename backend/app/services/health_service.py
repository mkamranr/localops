"""Dependency health checks for readiness and the dashboard."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import Settings
from app.database.session import get_engine
from app.schemas.audit import ServiceHealth

_TIMEOUT = 3.0


async def _timed(name: str, check: Callable[[], Awaitable[None]]) -> ServiceHealth:
    start = time.perf_counter()
    try:
        await asyncio.wait_for(check(), timeout=_TIMEOUT)
        return ServiceHealth(
            name=name, status="ok", latency_ms=round((time.perf_counter() - start) * 1000, 1)
        )
    except Exception as exc:
        # Only expose the exception type: messages may contain hostnames or credentials.
        return ServiceHealth(name=name, status="error", detail=type(exc).__name__)


async def check_all(settings: Settings, redis: Redis) -> list[ServiceHealth]:
    async def postgres() -> None:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def redis_ping() -> None:
        await redis.ping()

    async def qdrant() -> None:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{settings.qdrant_url.rstrip('/')}/readyz")
            resp.raise_for_status()

    return list(
        await asyncio.gather(
            _timed("postgres", postgres), _timed("redis", redis_ping), _timed("qdrant", qdrant)
        )
    )
