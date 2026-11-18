"""Database engine and tenant-scoped sessions.

Tenant isolation is enforced by PostgreSQL row-level security. Every unit of work runs
in a transaction that sets ``app.tenant_id`` (transaction-local) before any query, so a
query issued without tenant context returns no tenant-owned rows at all.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        _engine = create_async_engine(
            settings.app_database_url,
            pool_size=settings.db_pool_size,
            pool_pre_ping=True,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine not initialised")
    return _engine


async def set_tenant_context(session: AsyncSession, tenant_id: uuid.UUID | None) -> None:
    """Set (or clear) the RLS tenant for the current transaction.

    Pending ORM changes are flushed first so they're checked against the *previous*
    tenant context, never the new one.
    """
    await session.flush()
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id) if tenant_id else ""},
    )


async def set_platform_admin(session: AsyncSession, enabled: bool) -> None:
    """Allow cross-tenant access to the ``tenants`` table for this transaction.

    Callers MUST have verified the ``tenant.manage`` permission in code first.
    """
    await session.execute(
        text("SELECT set_config('app.platform_admin', :v, true)"),
        {"v": "on" if enabled else ""},
    )


@asynccontextmanager
async def tenant_session(
    tenant_id: uuid.UUID | None, *, platform_admin: bool = False
) -> AsyncIterator[AsyncSession]:
    """Open a session with a transaction bound to ``tenant_id``.

    Commits on success and rolls back on any exception.
    """
    if _session_factory is None:
        raise RuntimeError("Database engine not initialised")
    async with _session_factory() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        if platform_admin:
            await set_platform_admin(session, True)
        yield session
