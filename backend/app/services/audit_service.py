"""Audit logging. Records are written in the caller's transaction."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog
from app.schemas.common import PageParams
from app.services.context import ClientInfo, Principal


async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    action: str,
    actor: Principal | None = None,
    user_id: uuid.UUID | None = None,
    username: str | None = None,
    outcome: str = "success",
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
    client: ClientInfo | None = None,
) -> None:
    """Append an audit record for ``tenant_id``.

    The session's RLS tenant context must equal ``tenant_id``. The row is flushed
    immediately so a later tenant-context switch cannot misattribute it.
    """
    client = client or ClientInfo()
    entry = AuditLog(
        tenant_id=tenant_id,
        user_id=user_id if user_id is not None else (actor.user_id if actor else None),
        username=username if username is not None else (actor.username if actor else None),
        action=action,
        outcome=outcome,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details or {},
        ip_address=client.ip_address,
        user_agent=(client.user_agent or "")[:500] or None,
        request_id=client.request_id,
    )
    session.add(entry)
    await session.flush()


async def list_logs(
    session: AsyncSession,
    page: PageParams,
    *,
    action: str | None = None,
    user_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    outcome: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[list[AuditLog], int]:
    stmt = select(AuditLog)
    if action:
        # Prefix match so "auth." returns all auth events.
        escaped = action.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(AuditLog.action.like(f"{escaped}%", escape="\\"))
    if user_id:
        stmt = stmt.where(AuditLog.user_id == user_id)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if outcome:
        stmt = stmt.where(AuditLog.outcome == outcome)
    if date_from:
        stmt = stmt.where(AuditLog.created_at >= date_from)
    if date_to:
        stmt = stmt.where(AuditLog.created_at <= date_to)

    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .offset(page.offset)
                .limit(page.page_size)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total
