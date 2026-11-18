"""User and role management within the caller's tenant."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.core.security import hash_password
from app.models import RefreshToken, Role, User
from app.repositories import identity as repo
from app.schemas.common import PageParams
from app.schemas.identity import UserCreate, UserUpdate
from app.services import audit_service as audit
from app.services.context import RequestContext
from app.services.tenant_service import create_user_with_roles


@dataclass(slots=True)
class UserWithRoles:
    user: User
    roles: list[Role]


@dataclass(slots=True)
class RoleWithDetails:
    role: Role
    permissions: list[str]
    user_count: int


async def _load_roles_checked(ctx: RequestContext, role_ids: list[uuid.UUID]) -> list[Role]:
    roles = await repo.get_roles(ctx.session, ctx.principal.tenant_id, role_ids)
    if len(roles) != len(set(role_ids)):
        raise NotFoundError("One or more roles were not found", details={"field": "role_ids"})
    return roles


async def _guard_escalation(ctx: RequestContext, role_ids: set[uuid.UUID]) -> None:
    """An actor may only grant or revoke roles whose permissions they already hold."""
    if not role_ids:
        return
    perms = await repo.role_permission_codes(ctx.session, list(role_ids))
    exceeding = sorted({c for codes in perms.values() for c in codes} - ctx.principal.permissions)
    if exceeding:
        raise PermissionDeniedError(
            "You cannot grant or revoke roles with permissions you do not hold",
            details={"permissions": exceeding},
        )


async def _revoke_sessions(ctx: RequestContext, user_id: uuid.UUID) -> None:
    await ctx.session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def list_users(
    ctx: RequestContext, page: PageParams, *, q: str | None, status: str | None
) -> tuple[list[UserWithRoles], int]:
    stmt = repo.users_query(ctx.principal.tenant_id, q=q, status=status)
    total = (
        await ctx.session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    users = list(
        (
            await ctx.session.execute(
                stmt.order_by(User.username).offset(page.offset).limit(page.page_size)
            )
        ).scalars()
    )
    roles = await repo.user_roles(ctx.session, [u.id for u in users])
    return [UserWithRoles(u, roles[u.id]) for u in users], total


async def get_user(ctx: RequestContext, user_id: uuid.UUID) -> UserWithRoles:
    user = await repo.get_user(ctx.session, ctx.principal.tenant_id, user_id)
    if user is None:
        raise NotFoundError("User not found")
    roles = await repo.user_roles(ctx.session, [user.id])
    return UserWithRoles(user, roles[user.id])


async def create_user(ctx: RequestContext, data: UserCreate) -> UserWithRoles:
    tenant_id = ctx.principal.tenant_id
    if data.role_ids:
        ctx.principal.require("role.assign")
    conflict = await repo.username_or_email_taken(
        ctx.session, tenant_id, username=data.username, email=data.email
    )
    if conflict:
        raise ConflictError(
            f"A user with this {conflict} already exists", details={"field": conflict}
        )
    roles = await _load_roles_checked(ctx, data.role_ids)
    await _guard_escalation(ctx, {r.id for r in roles})
    user = await create_user_with_roles(
        ctx.session,
        tenant_id=tenant_id,
        username=data.username,
        email=data.email,
        password=data.password,
        display_name=data.display_name,
        roles=roles,
        must_change_password=data.must_change_password,
    )
    await audit.record(
        ctx.session,
        tenant_id=tenant_id,
        actor=ctx.principal,
        action="user.create",
        entity_type="user",
        entity_id=user.id,
        details={"username": user.username, "roles": [r.name for r in roles]},
        client=ctx.client,
    )
    return UserWithRoles(user, roles)


async def update_user(ctx: RequestContext, user_id: uuid.UUID, data: UserUpdate) -> UserWithRoles:
    result = await get_user(ctx, user_id)
    user = result.user
    is_self = user.id == ctx.principal.user_id
    changes: dict[str, object] = {}

    if data.email is not None and data.email != user.email:
        if await repo.username_or_email_taken(
            ctx.session, user.tenant_id, email=data.email, exclude_user_id=user.id
        ):
            raise ConflictError("A user with this email already exists", details={"field": "email"})
        changes["email"] = {"from": user.email, "to": data.email}
        user.email = data.email
    if data.display_name is not None and data.display_name != user.display_name:
        changes["display_name"] = {"from": user.display_name, "to": data.display_name}
        user.display_name = data.display_name
    if data.status is not None and data.status != user.status:
        if is_self:
            raise PermissionDeniedError("You cannot change your own account status")
        await _guard_escalation(ctx, {r.id for r in result.roles})
        changes["status"] = {"from": user.status, "to": data.status}
        user.status = data.status
        if data.status == "disabled":
            await _revoke_sessions(ctx, user.id)
    if data.password is not None:
        if is_self:
            raise PermissionDeniedError("Use change-password to change your own password")
        await _guard_escalation(ctx, {r.id for r in result.roles})
        user.password_hash = hash_password(data.password)
        user.password_changed_at = datetime.now(UTC)
        user.must_change_password = True
        await _revoke_sessions(ctx, user.id)
        changes["password"] = "reset"  # noqa: S105

    if changes:
        await ctx.session.flush()
        await audit.record(
            ctx.session,
            tenant_id=user.tenant_id,
            actor=ctx.principal,
            action="user.update",
            entity_type="user",
            entity_id=user.id,
            details={"username": user.username, "changes": changes},
            client=ctx.client,
        )
    return result


async def delete_user(ctx: RequestContext, user_id: uuid.UUID) -> None:
    result = await get_user(ctx, user_id)
    if result.user.id == ctx.principal.user_id:
        raise PermissionDeniedError("You cannot delete your own account")
    await _guard_escalation(ctx, {r.id for r in result.roles})
    result.user.status = "deleted"
    await _revoke_sessions(ctx, result.user.id)
    await repo.replace_user_roles(ctx.session, result.user.tenant_id, result.user.id, [])
    await audit.record(
        ctx.session,
        tenant_id=result.user.tenant_id,
        actor=ctx.principal,
        action="user.delete",
        entity_type="user",
        entity_id=result.user.id,
        details={"username": result.user.username},
        client=ctx.client,
    )


async def set_user_roles(
    ctx: RequestContext, user_id: uuid.UUID, role_ids: list[uuid.UUID]
) -> UserWithRoles:
    result = await get_user(ctx, user_id)
    if result.user.id == ctx.principal.user_id:
        raise PermissionDeniedError("You cannot change your own roles")
    new_roles = await _load_roles_checked(ctx, role_ids)
    old_ids = {r.id for r in result.roles}
    new_ids = {r.id for r in new_roles}
    await _guard_escalation(ctx, old_ids ^ new_ids)  # roles being added or removed
    if old_ids != new_ids:
        await repo.replace_user_roles(ctx.session, result.user.tenant_id, result.user.id, new_ids)
        await audit.record(
            ctx.session,
            tenant_id=result.user.tenant_id,
            actor=ctx.principal,
            action="user.roles.update",
            entity_type="user",
            entity_id=result.user.id,
            details={
                "username": result.user.username,
                "from": sorted(r.name for r in result.roles),
                "to": sorted(r.name for r in new_roles),
            },
            client=ctx.client,
        )
    return UserWithRoles(result.user, sorted(new_roles, key=lambda r: r.name))


async def list_roles(ctx: RequestContext) -> list[RoleWithDetails]:
    roles = await repo.get_roles(ctx.session, ctx.principal.tenant_id)
    ids = [r.id for r in roles]
    perms = await repo.role_permission_codes(ctx.session, ids)
    counts = await repo.role_user_counts(ctx.session, ids)
    return [RoleWithDetails(r, sorted(perms[r.id]), counts.get(r.id, 0)) for r in roles]


async def get_role(ctx: RequestContext, role_id: uuid.UUID) -> RoleWithDetails:
    roles = await repo.get_roles(ctx.session, ctx.principal.tenant_id, [role_id])
    if not roles:
        raise NotFoundError("Role not found")
    perms = await repo.role_permission_codes(ctx.session, [role_id])
    counts = await repo.role_user_counts(ctx.session, [role_id])
    return RoleWithDetails(roles[0], sorted(perms[role_id]), counts.get(role_id, 0))
