"""Tenant lifecycle: creation (with role seeding and first admin), update, disable."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.core.permissions import SUPER_ADMIN, TENANT_ADMIN, roles_for_tenant
from app.core.security import hash_password
from app.database.session import set_platform_admin, set_tenant_context
from app.models import SYSTEM_TENANT_SLUG, Permission, Role, RolePermission, Tenant, User, UserRole
from app.repositories import identity as repo
from app.schemas.common import PageParams
from app.schemas.identity import TenantCreate, TenantUpdate
from app.services import audit_service as audit
from app.services.context import RequestContext


async def seed_system_roles(
    session: AsyncSession, tenant_id: uuid.UUID, *, is_system_tenant: bool
) -> dict[str, Role]:
    """Create any missing built-in roles for the tenant (idempotent).

    Requires the session's RLS context to be ``tenant_id``.
    """
    perm_ids = {p.code: p.id for p in (await session.execute(select(Permission))).scalars()}
    existing = {r.name: r for r in await repo.get_roles(session, tenant_id)}
    roles: dict[str, Role] = {}
    for name, (description, codes) in roles_for_tenant(is_system_tenant=is_system_tenant).items():
        role = existing.get(name)
        if role is None:
            role = Role(tenant_id=tenant_id, name=name, description=description, is_system=True)
            session.add(role)
            await session.flush()
            for code in sorted(codes):
                if code in perm_ids:
                    session.add(
                        RolePermission(
                            role_id=role.id, permission_id=perm_ids[code], tenant_id=tenant_id
                        )
                    )
        roles[name] = role
    await session.flush()
    return roles


async def create_user_with_roles(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    username: str,
    email: str,
    password: str,
    display_name: str | None,
    roles: list[Role],
    must_change_password: bool,
) -> User:
    user = User(
        tenant_id=tenant_id,
        username=username,
        email=email,
        password_hash=hash_password(password),
        display_name=display_name or username,
        status="active",
        must_change_password=must_change_password,
        password_changed_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    for role in roles:
        session.add(UserRole(user_id=user.id, role_id=role.id, tenant_id=tenant_id))
    await session.flush()
    return user


def _require_platform_admin(ctx: RequestContext) -> None:
    ctx.principal.require("tenant.manage")
    if ctx.principal.tenant_slug != SYSTEM_TENANT_SLUG:
        raise PermissionDeniedError("Tenant management is restricted to the system tenant")


async def _enable_platform_scope(ctx: RequestContext) -> None:
    _require_platform_admin(ctx)
    await set_platform_admin(ctx.session, True)


async def list_tenants(ctx: RequestContext, page: PageParams) -> tuple[list[Tenant], int]:
    await _enable_platform_scope(ctx)
    total = (await ctx.session.execute(select(func.count()).select_from(Tenant))).scalar_one()
    rows = (
        (
            await ctx.session.execute(
                select(Tenant).order_by(Tenant.created_at).offset(page.offset).limit(page.page_size)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def get_tenant(ctx: RequestContext, tenant_id: uuid.UUID) -> Tenant:
    await _enable_platform_scope(ctx)
    tenant = await repo.get_tenant(ctx.session, tenant_id)
    if tenant is None:
        raise NotFoundError("Tenant not found")
    return tenant


async def create_tenant(ctx: RequestContext, data: TenantCreate) -> Tenant:
    await _enable_platform_scope(ctx)
    session, actor = ctx.session, ctx.principal
    if data.slug == SYSTEM_TENANT_SLUG or await repo.get_tenant_by_slug(session, data.slug):
        raise ConflictError("A tenant with this slug already exists", details={"field": "slug"})

    tenant = Tenant(name=data.name, slug=data.slug, status="active", settings={})
    session.add(tenant)
    await session.flush()
    await audit.record(
        session,
        tenant_id=actor.tenant_id,
        actor=actor,
        action="tenant.create",
        entity_type="tenant",
        entity_id=tenant.id,
        details={"slug": tenant.slug, "name": tenant.name},
        client=ctx.client,
    )

    # Seed the new tenant inside its own RLS context, then switch back.
    await set_tenant_context(session, tenant.id)
    try:
        roles = await seed_system_roles(session, tenant.id, is_system_tenant=False)
        admin = await create_user_with_roles(
            session,
            tenant_id=tenant.id,
            username=data.admin.username,
            email=data.admin.email,
            password=data.admin.password,
            display_name=data.admin.display_name,
            roles=[roles[TENANT_ADMIN]],
            must_change_password=True,
        )
        await audit.record(
            session,
            tenant_id=tenant.id,
            action="user.create",
            user_id=None,
            username=f"{actor.username}@{actor.tenant_slug}",
            entity_type="user",
            entity_id=admin.id,
            details={"username": admin.username, "roles": [TENANT_ADMIN], "via": "tenant.create"},
            client=ctx.client,
        )
    finally:
        await set_tenant_context(session, actor.tenant_id)
    return tenant


async def update_tenant(ctx: RequestContext, tenant_id: uuid.UUID, data: TenantUpdate) -> Tenant:
    tenant = await get_tenant(ctx, tenant_id)
    changes: dict[str, object] = {}
    if data.name is not None and data.name != tenant.name:
        changes["name"] = {"from": tenant.name, "to": data.name}
        tenant.name = data.name
    if data.status is not None and data.status != tenant.status:
        if tenant.slug == SYSTEM_TENANT_SLUG and data.status != "active":
            raise PermissionDeniedError("The system tenant cannot be disabled")
        changes["status"] = {"from": tenant.status, "to": data.status}
        tenant.status = data.status
    if changes:
        await ctx.session.flush()
        await audit.record(
            ctx.session,
            tenant_id=ctx.principal.tenant_id,
            actor=ctx.principal,
            action="tenant.update",
            entity_type="tenant",
            entity_id=tenant.id,
            details={"slug": tenant.slug, "changes": changes},
            client=ctx.client,
        )
    return tenant


async def ensure_platform_bootstrap(
    session: AsyncSession,
    *,
    admin_username: str,
    admin_email: str,
    admin_password: str,
    create_default_tenant: bool,
) -> list[str]:
    """Idempotently create the system tenant, its Super Administrator and (optionally)
    a ``default`` tenant. Returns human-readable notes about what was created."""
    notes: list[str] = []
    await set_platform_admin(session, True)

    async def _ensure_tenant(slug: str, name: str) -> tuple[Tenant, bool]:
        tenant = await repo.get_tenant_by_slug(session, slug)
        if tenant:
            return tenant, False
        tenant = Tenant(name=name, slug=slug, status="active", settings={})
        session.add(tenant)
        await session.flush()
        notes.append(f"created tenant '{slug}'")
        return tenant, True

    system, _ = await _ensure_tenant(SYSTEM_TENANT_SLUG, "System")
    await set_tenant_context(session, system.id)
    roles = await seed_system_roles(session, system.id, is_system_tenant=True)
    has_admin = (
        await session.execute(
            select(UserRole.user_id).where(UserRole.role_id == roles[SUPER_ADMIN].id).limit(1)
        )
    ).first()
    if not has_admin:
        if await repo.username_or_email_taken(
            session, system.id, username=admin_username, email=admin_email
        ):
            raise ConflictError("Bootstrap admin username/email already used in system tenant")
        admin = await create_user_with_roles(
            session,
            tenant_id=system.id,
            username=admin_username,
            email=admin_email,
            password=admin_password,
            display_name="Platform Administrator",
            roles=[roles[SUPER_ADMIN]],
            must_change_password=True,
        )
        await audit.record(
            session,
            tenant_id=system.id,
            action="user.create",
            username="bootstrap",
            entity_type="user",
            entity_id=admin.id,
            details={"username": admin.username, "roles": [SUPER_ADMIN], "via": "bootstrap"},
        )
        notes.append(f"created super administrator '{admin_username}' in tenant 'system'")

    if create_default_tenant:
        default, created = await _ensure_tenant("default", "Default")
        await set_tenant_context(session, default.id)
        await seed_system_roles(session, default.id, is_system_tenant=False)
        if created:
            await audit.record(
                session,
                tenant_id=default.id,
                action="tenant.create",
                username="bootstrap",
                entity_type="tenant",
                entity_id=default.id,
                details={"via": "bootstrap"},
            )
    return notes
