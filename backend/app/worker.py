"""Background worker (arq, Redis-backed).

Runs investigation graphs, infrastructure discovery runs, the discovery scheduler
(every minute) and a heartbeat cron. On startup it verifies the checkpoint schema and
re-enqueues investigations that were queued or running when the previous worker
stopped; each run resumes from its last persisted checkpoint.

Run:   arq app.worker.WorkerSettings
Check: arq --check app.worker.WorkerSettings
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

# Must be set before LangChain/LangGraph modules are imported (air-gapped).
os.environ.setdefault("LANGSMITH_TRACING", "false")
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")

from arq import Retry, cron
from arq.connections import RedisSettings
from arq.worker import func
from sqlalchemy import text

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.redis import global_key
from app.database.session import dispose_engine, get_engine, init_engine
from app.discovery.engine import run_discovery
from app.discovery.scheduler import schedule_due
from app.graphs.checkpointer import verify_checkpoint_schema
from app.graphs.dispatch import DISCOVERY_JOB, JOB_NAME, QUEUE_NAME, ArqDispatcher
from app.graphs.runner import InvestigationLockedError, run_investigation

log = get_logger("localops.worker")


async def heartbeat(ctx: dict[str, Any]) -> None:
    await ctx["redis"].set(global_key("worker", "heartbeat"), datetime.now(UTC).isoformat(), ex=180)


async def run_investigation_job(
    ctx: dict[str, Any], tenant_id: str, investigation_id: str, resume: str | None = None
) -> str | None:
    try:
        return await run_investigation(
            get_settings(),
            tenant_id=uuid.UUID(tenant_id),
            investigation_id=uuid.UUID(investigation_id),
            redis=ctx["redis"],
            resume=resume,
        )
    except InvestigationLockedError:
        # Another worker holds the run lock (or a crashed one's lock is expiring).
        raise Retry(defer=20) from None


async def run_discovery_job(ctx: dict[str, Any], tenant_id: str, run_id: str) -> str:
    return await run_discovery(
        get_settings(),
        tenant_id=uuid.UUID(tenant_id),
        run_id=uuid.UUID(run_id),
        redis=ctx["redis"],
    )


async def discovery_scheduler(ctx: dict[str, Any]) -> int:
    return await schedule_due(ArqDispatcher(ctx["redis"]))


async def recover(ctx: dict[str, Any]) -> int:
    """Re-enqueue investigations interrupted by a restart."""
    async with get_engine().connect() as conn:
        result = await conn.execute(
            text("SELECT id, tenant_id FROM worker_recoverable_investigations()")
        )
        rows = result.all()
    dispatcher = ArqDispatcher(ctx["redis"])
    for inv_id, tenant_id in rows:
        await dispatcher.enqueue(tenant_id, inv_id)
    if rows:
        log.info("investigations_recovered", count=len(rows))
    return len(rows)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    init_engine(settings)
    await verify_checkpoint_schema(settings)
    await recover(ctx)
    log.info("worker_startup")


async def shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engine()
    log.info("worker_shutdown")


def _job_timeout() -> int:
    return get_settings().investigation_timeout_seconds + 120


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    queue_name = QUEUE_NAME
    functions: ClassVar[list[Any]] = [
        func(run_investigation_job, name=JOB_NAME),
        func(run_discovery_job, name=DISCOVERY_JOB, max_tries=1, timeout=3600),
    ]
    cron_jobs: ClassVar[list[Any]] = [
        cron(heartbeat, second={0}, run_at_startup=True),
        cron(discovery_scheduler, second={30}, run_at_startup=False, unique=True),
    ]
    on_startup = startup
    on_shutdown = shutdown
    health_check_interval = 30
    health_check_key = "localops:worker:health"
    max_jobs = 10
    max_tries = 10
    job_timeout = _job_timeout()
