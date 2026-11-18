"""Data access for tenants, users, roles and permissions.

All queries run under the caller's RLS tenant context; explicit ``tenant_id`` filters
are added as defence in depth, never as the only isolation mechanism.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import Select, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Permission, Role, RolePermission, Tenant, User, UserRole


async def resolve_tenant_slug(session: AsyncSession, slug: str) -> tuple[uuid.UUID, str] | None:
    """Look up a tenant before any tenant context exists (SECURITY DEFINER function)."""
    row = (
        await session.execute(
            text("SELECT id, status FROM auth_resolve_tenant(:slug)"), {"slug": slug}
        )
    ).first()
    return (row.id, row.status) if row else None


async def get_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant | None:
    return await session.get(Tenant, tenant_id)


async def get_tenant_by_slug(session: AsyncSession, slug: str) -> Tenant | None:
    return (await session.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()


async def get_user(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    include_deleted: bool = False,
) -> User | None:
    stmt = select(User).where(User.id == user_id, User.tenant_id == tenant_id)
    if not include_deleted:
        stmt = stmt.where(User.status != "deleted")
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_user_by_username(
    session: AsyncSession, tenant_id: uuid.UUID, username: str
) -> User | None:
    stmt = select(User).where(
        User.tenant_id == tenant_id, User.username == username, User.status != "deleted"
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def username_or_email_taken(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    username: str | None = None,
    email: str | None = None,
    exclude_user_id: uuid.UUID | None = None,
) -> str | None:
    """Return the conflicting field name, if any. Deleted users still reserve names."""
    for field, value in (("username", username), ("email", email)):
        if value is None:
            continue
        stmt = select(User.id).where(User.tenant_id == tenant_id, getattr(User, field) == value)
        if exclude_user_id:
            stmt = stmt.where(User.id != exclude_user_id)
        if (await session.execute(stmt.limit(1))).first():
            return field
    return None


async def user_permissions(session: AsyncSession, user_id: uuid.UUID) -> frozenset[str]:
    stmt = (
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .where(UserRole.user_id == user_id)
        .distinct()
    )
    return frozenset((await session.execute(stmt)).scalars().all())


async def user_roles(
    session: AsyncSession, user_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[Role]]:
    result: dict[uuid.UUID, list[Role]] = {uid: [] for uid in user_ids}
    if not user_ids:
        return result
    stmt = (
        select(UserRole.user_id, Role)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id.in_(user_ids))
        .order_by(Role.name)
    )
    for uid, role in (await session.execute(stmt)).all():
        result[uid].append(role)
    return result


def users_query(tenant_id: uuid.UUID, *, q: str | None, status: str | None) -> Select[User]:
    stmt = select(User).where(User.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(User.status == status)
    else:
        stmt = stmt.where(User.status != "deleted")
    if q:
        like = f"%{q.lower().replace('%', '').replace('_', '')}%"
        stmt = stmt.where(
            func.lower(User.username).like(like)
            | func.lower(User.email).like(like)
            | func.lower(User.display_name).like(like)
        )
    return stmt


async def get_roles(
    session: AsyncSession, tenant_id: uuid.UUID, role_ids: Iterable[uuid.UUID] | None = None
) -> list[Role]:
    stmt = select(Role).where(Role.tenant_id == tenant_id)
    if role_ids is not None:
        stmt = stmt.where(Role.id.in_(list(role_ids)))
    return list((await session.execute(stmt.order_by(Role.name))).scalars().all())


async def role_permission_codes(
    session: AsyncSession, role_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, set[str]]:
    result: dict[uuid.UUID, set[str]] = {rid: set() for rid in role_ids}
    if not role_ids:
        return result
    stmt = (
        select(RolePermission.role_id, Permission.code)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(RolePermission.role_id.in_(role_ids))
    )
    for rid, code in (await session.execute(stmt)).all():
        result[rid].add(code)
    return result


async def role_user_counts(
    session: AsyncSession, role_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not role_ids:
        return {}
    stmt = (
        select(UserRole.role_id, func.count())
        .join(User, User.id == UserRole.user_id)
        .where(UserRole.role_id.in_(role_ids), User.status != "deleted")
        .group_by(UserRole.role_id)
    )
    return {rid: n for rid, n in (await session.execute(stmt)).all()}


async def replace_user_roles(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, role_ids: Iterable[uuid.UUID]
) -> None:
    await session.execute(delete(UserRole).where(UserRole.user_id == user_id))
    for rid in set(role_ids):
        session.add(UserRole(user_id=user_id, role_id=rid, tenant_id=tenant_id))
    await session.flush()


async def all_permissions(session: AsyncSession) -> list[Permission]:
    return list(
        (await session.execute(select(Permission).order_by(Permission.code))).scalars().all()
    )
