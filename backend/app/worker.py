"""Background worker (arq, Redis-backed).

Phase 1 runs only a heartbeat cron so the container, health check and scheduling
path exist. Investigation and discovery jobs arrive in later phases.

Run:   arq app.worker.WorkerSettings
Check: arq --check app.worker.WorkerSettings
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.redis import global_key

log = get_logger("localops.worker")


async def heartbeat(ctx: dict[str, Any]) -> None:
    await ctx["redis"].set(global_key("worker", "heartbeat"), datetime.now(UTC).isoformat(), ex=180)


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging(get_settings().log_level)
    log.info("worker_startup")


async def shutdown(ctx: dict[str, Any]) -> None:
    log.info("worker_shutdown")


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    queue_name = "localops:queue"
    functions: ClassVar[list[Any]] = []
    cron_jobs: ClassVar[list[Any]] = [cron(heartbeat, second={0}, run_at_startup=True)]
    on_startup = startup
    on_shutdown = shutdown
    health_check_interval = 30
    health_check_key = "localops:worker:health"
    max_jobs = 10
    job_timeout = 600
