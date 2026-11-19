"""Scheduled discovery: enqueue runs for integrations whose schedule is due."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.logging import get_logger
from app.database.session import get_engine, tenant_session
from app.graphs.dispatch import InvestigationDispatcher
from app.models import Integration
from app.services.integration_service import active_run, create_run

log = get_logger("localops.discovery.scheduler")


async def due_integrations() -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(integration id, tenant id) pairs, via the SECURITY DEFINER function."""
    async with get_engine().connect() as conn:
        result = await conn.execute(text("SELECT id, tenant_id FROM worker_due_integrations()"))
        return [(r[0], r[1]) for r in result.all()]


async def schedule_due(dispatcher: InvestigationDispatcher) -> int:
    now = datetime.now(UTC)
    queued = 0
    for integration_id, tenant_id in await due_integrations():
        run_id: uuid.UUID | None = None
        async with tenant_session(tenant_id) as s:
            integration = await s.get(Integration, integration_id)
            if integration is None or not integration.enabled or not integration.schedule_minutes:
                continue
            # Advance first so a failing integration is not retried every minute.
            integration.next_run_at = now + timedelta(minutes=integration.schedule_minutes)
            if integration.targets and not await active_run(s, integration.id):
                run = await create_run(s, integration, trigger="scheduled", triggered_by=None)
                run_id = run.id
        if run_id is not None:
            await dispatcher.enqueue_discovery(tenant_id, run_id)
            queued += 1
    if queued:
        log.info("discovery_scheduled", runs=queued)
    return queued
