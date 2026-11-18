"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, Response
from redis.asyncio import Redis

from app.api.dependencies import client_info, get_context, redis_dep, settings_dep
from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.repositories import identity as repo
from app.schemas.identity import (
    ChangePasswordRequest,
    LoginRequest,
    MeResponse,
    TenantBrief,
    TokenResponse,
)
from app.services import auth_service
from app.services.auth_service import IssuedTokens
from app.services.context import ClientInfo, RequestContext

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "localops_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"


def _set_refresh_cookie(response: Response, settings: Settings, tokens: IssuedTokens) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        tokens.refresh_token,
        max_age=settings.refresh_token_ttl_days * 86400,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


def _token_response(response: Response, settings: Settings, tokens: IssuedTokens) -> TokenResponse:
    _set_refresh_cookie(response, settings, tokens)
    response.headers["Cache-Control"] = "no-store"
    return TokenResponse(access_token=tokens.access_token, expires_in=tokens.expires_in)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    response: Response,
    settings: Settings = Depends(settings_dep),
    redis: Redis = Depends(redis_dep),
    client: ClientInfo = Depends(client_info),
) -> TokenResponse:
    tokens = await auth_service.login(
        settings,
        redis,
        tenant_slug=body.tenant,
        username=body.username,
        password=body.password,
        client=client,
    )
    return _token_response(response, settings, tokens)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    response: Response,
    refresh_token: str | None = Cookie(None, alias=REFRESH_COOKIE),
    settings: Settings = Depends(settings_dep),
    redis: Redis = Depends(redis_dep),
    client: ClientInfo = Depends(client_info),
) -> TokenResponse:
    if not refresh_token:
        raise AuthenticationError("No refresh token", code="invalid_refresh_token")
    try:
        tokens = await auth_service.refresh(
            settings, redis, refresh_token=refresh_token, client=client
        )
    except AuthenticationError:
        response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
        raise
    return _token_response(response, settings, tokens)


@router.post("/logout", status_code=204)
async def logout(
    response: Response,
    refresh_token: str | None = Cookie(None, alias=REFRESH_COOKIE),
    ctx: RequestContext = Depends(get_context),
    redis: Redis = Depends(redis_dep),
) -> Response:
    await auth_service.logout(ctx, redis, refresh_token=refresh_token)
    response = Response(status_code=204)
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
    return response


@router.get("/me", response_model=MeResponse)
async def me(ctx: RequestContext = Depends(get_context)) -> MeResponse:
    p = ctx.principal
    user = await repo.get_user(ctx.session, p.tenant_id, p.user_id)
    tenant = await repo.get_tenant(ctx.session, p.tenant_id)
    assert user is not None and tenant is not None  # guaranteed by get_context  # noqa: S101
    return MeResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        must_change_password=user.must_change_password,
        tenant=TenantBrief.model_validate(tenant),
        roles=list(p.roles),
        permissions=sorted(p.permissions),
    )


@router.post("/change-password", response_model=TokenResponse)
async def change_password(
    body: ChangePasswordRequest,
    response: Response,
    ctx: RequestContext = Depends(get_context),
    settings: Settings = Depends(settings_dep),
    redis: Redis = Depends(redis_dep),
) -> TokenResponse:
    tokens = await auth_service.change_password(
        ctx,
        settings,
        redis,
        current_password=body.current_password,
        new_password=body.new_password,
    )
    return _token_response(response, settings, tokens)
