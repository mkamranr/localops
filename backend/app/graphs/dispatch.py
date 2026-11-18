"""Hands investigation runs to an executor.

Production uses the arq worker (``ArqDispatcher``); tests and single-process setups
can use ``InlineDispatcher``, which runs the graph as an in-process task.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Protocol

from arq.connections import ArqRedis
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.logging import get_logger
from app.graphs.runtime import ClientFactory
from app.llm.gateway import ModelClient

log = get_logger("localops.dispatch")

JOB_NAME = "run_investigation"
QUEUE_NAME = "localops:queue"


class InvestigationDispatcher(Protocol):
    async def enqueue(
        self, tenant_id: uuid.UUID, investigation_id: uuid.UUID, resume: str | None = None
    ) -> None: ...

    async def close(self) -> None: ...


class ArqDispatcher:
    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def enqueue(
        self, tenant_id: uuid.UUID, investigation_id: uuid.UUID, resume: str | None = None
    ) -> None:
        await self._pool.enqueue_job(
            JOB_NAME,
            str(tenant_id),
            str(investigation_id),
            resume,
            _job_id=f"investigation:{investigation_id}:{uuid.uuid4().hex[:8]}",
            _queue_name=QUEUE_NAME,
        )

    async def close(self) -> None:
        await self._pool.aclose()


class InlineDispatcher:
    def __init__(
        self,
        settings: Settings,
        redis: Redis | None,
        client_factory: ClientFactory = ModelClient,
    ) -> None:
        self.settings = settings
        self.redis = redis
        self.client_factory = client_factory
        self.tasks: set[asyncio.Task[object]] = set()

    async def enqueue(
        self, tenant_id: uuid.UUID, investigation_id: uuid.UUID, resume: str | None = None
    ) -> None:
        from app.graphs.runner import run_investigation

        task: asyncio.Task[object] = asyncio.create_task(
            run_investigation(
                self.settings,
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                redis=self.redis,
                resume=resume,
                client_factory=self.client_factory,
            )
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def drain(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
