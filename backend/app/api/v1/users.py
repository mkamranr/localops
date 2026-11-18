"""User, role and permission endpoints (tenant-scoped)."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response

from app.api.dependencies import get_context, require_permission
from app.repositories import identity as repo
from app.schemas.common import Page, PageParams
from app.schemas.identity import (
    PermissionOut,
    RoleBrief,
    RoleOut,
    UserCreate,
    UserOut,
    UserRolesUpdate,
    UserUpdate,
)
from app.services import user_service
from app.services.context import RequestContext
from app.services.user_service import RoleWithDetails, UserWithRoles

router = APIRouter(tags=["users"])


def _user_out(u: UserWithRoles) -> UserOut:
    out = UserOut.model_validate(u.user)
    out.roles = [RoleBrief.model_validate(r) for r in u.roles]
    return out


def _role_out(r: RoleWithDetails) -> RoleOut:
    return RoleOut(
        id=r.role.id,
        name=r.role.name,
        description=r.role.description,
        is_system=r.role.is_system,
        permissions=r.permissions,
        user_count=r.user_count,
    )


@router.get("/users", response_model=Page[UserOut])
async def list_users(
    page: PageParams = Depends(),
    q: str | None = Query(None, max_length=100),
    status: Literal["active", "disabled", "deleted"] | None = None,
    ctx: RequestContext = Depends(require_permission("user.read")),
) -> Page[UserOut]:
    rows, total = await user_service.list_users(ctx, page, q=q, status=status)
    return Page(
        items=[_user_out(u) for u in rows], total=total, page=page.page, page_size=page.page_size
    )


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    body: UserCreate, ctx: RequestContext = Depends(require_permission("user.create"))
) -> UserOut:
    return _user_out(await user_service.create_user(ctx, body))


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(
    user_id: uuid.UUID, ctx: RequestContext = Depends(require_permission("user.read"))
) -> UserOut:
    return _user_out(await user_service.get_user(ctx, user_id))


@router.put("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    ctx: RequestContext = Depends(require_permission("user.update")),
) -> UserOut:
    return _user_out(await user_service.update_user(ctx, user_id, body))


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: uuid.UUID, ctx: RequestContext = Depends(require_permission("user.delete"))
) -> Response:
    await user_service.delete_user(ctx, user_id)
    return Response(status_code=204)


@router.put("/users/{user_id}/roles", response_model=UserOut)
async def set_user_roles(
    user_id: uuid.UUID,
    body: UserRolesUpdate,
    ctx: RequestContext = Depends(require_permission("role.assign")),
) -> UserOut:
    return _user_out(await user_service.set_user_roles(ctx, user_id, body.role_ids))


@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    ctx: RequestContext = Depends(require_permission("role.read")),
) -> list[RoleOut]:
    return [_role_out(r) for r in await user_service.list_roles(ctx)]


@router.get("/roles/{role_id}", response_model=RoleOut)
async def get_role(
    role_id: uuid.UUID, ctx: RequestContext = Depends(require_permission("role.read"))
) -> RoleOut:
    return _role_out(await user_service.get_role(ctx, role_id))


@router.get("/permissions", response_model=list[PermissionOut])
async def list_permissions(ctx: RequestContext = Depends(get_context)) -> list[PermissionOut]:
    ctx.principal.require("role.read")
    return [PermissionOut.model_validate(p) for p in await repo.all_permissions(ctx.session)]
