"""Tenant-scoped LangGraph checkpointer (PostgreSQL).

Each investigation run gets its own psycopg connection as ``localops_app`` with
``app.tenant_id`` set at *session* level, so the RLS policies on the checkpoint
tables (keyed on the ``<tenant_id>:`` prefix of ``thread_id``) apply to every
statement the saver issues. The schema is created by Alembic revision 0002, never by
``saver.setup()`` at runtime (the app role has no DDL rights).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.postgres.base import BasePostgresSaver
from psycopg.rows import dict_row

from app.core.config import Settings


class CheckpointSchemaError(RuntimeError):
    pass


def thread_id(tenant_id: uuid.UUID, investigation_id: uuid.UUID) -> str:
    return f"{tenant_id}:{investigation_id}"


async def _connect(settings: Settings) -> psycopg.AsyncConnection[dict[str, object]]:
    return await psycopg.AsyncConnection.connect(
        settings.app_psycopg_conninfo,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    )


@asynccontextmanager
async def tenant_checkpointer(
    settings: Settings, tenant_id: uuid.UUID
) -> AsyncIterator[AsyncPostgresSaver]:
    conn = await _connect(settings)
    async with conn:
        await conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant_id),))
        yield AsyncPostgresSaver(conn)


async def verify_checkpoint_schema(settings: Settings) -> None:
    """Fail fast if the library expects a newer checkpoint schema than Alembic created."""
    expected = len(BasePostgresSaver.MIGRATIONS) - 1
    conn = await _connect(settings)
    async with conn:
        cur = await conn.execute("SELECT max(v) AS v FROM checkpoint_migrations")
        row = await cur.fetchone()
    applied = row["v"] if row else None
    if applied != expected:
        raise CheckpointSchemaError(
            f"Checkpoint schema version {applied} does not match langgraph-checkpoint-postgres "
            f"(expects {expected}); add an Alembic migration before upgrading the library"
        )
