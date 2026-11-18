"""FastAPI dependencies: settings, Redis, client info, authenticated request context."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError, ServiceUnavailableError
from app.core.logging import request_id_var
from app.core.security import decode_access_token
from app.database.session import tenant_session
from app.repositories import identity as repo
from app.services.auth_service import is_access_token_denied
from app.services.context import ClientInfo, Principal, RequestContext

_bearer = HTTPBearer(auto_error=False)


def settings_dep() -> Settings:
    return get_settings()


def redis_dep(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


def client_info(request: Request) -> ClientInfo:
    return ClientInfo(
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_id=request_id_var.get(),
    )


async def get_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(settings_dep),
    redis: Redis = Depends(redis_dep),
    client: ClientInfo = Depends(client_info),
) -> AsyncIterator[RequestContext]:
    """Authenticate the bearer token and open a session bound to the user's tenant.

    The transaction commits after the endpoint returns and rolls back on error.
    Permissions are always loaded from the database, never trusted from the token.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Authentication required")
    claims = decode_access_token(settings, credentials.credentials)
    try:
        denied = await is_access_token_denied(redis, claims.tenant_id, claims.jti)
    except Exception as exc:  # fail closed if the denylist can't be checked
        raise ServiceUnavailableError("Session store unavailable") from exc
    if denied:
        raise AuthenticationError("Session has been revoked", code="token_revoked")

    async with tenant_session(claims.tenant_id) as session:
        user = await repo.get_user(session, claims.tenant_id, claims.user_id)
        tenant = await repo.get_tenant(session, claims.tenant_id)
        if user is None or user.status != "active" or tenant is None or tenant.status != "active":
            raise AuthenticationError("Account is not active", code="account_inactive")
        if user.password_changed_at and claims.issued_at < int(
            user.password_changed_at.timestamp()
        ):
            raise AuthenticationError(
                "Session invalidated by password change", code="token_revoked"
            )
        permissions = await repo.user_permissions(session, user.id)
        roles = await repo.user_roles(session, [user.id])
        principal = Principal(
            user_id=user.id,
            username=user.username,
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            permissions=permissions,
            roles=tuple(r.name for r in roles[user.id]),
            jti=claims.jti,
            token_expires_at=claims.expires_at,
        )
        yield RequestContext(session=session, principal=principal, client=client)


def require_permission(
    *permissions: str,
) -> Callable[[RequestContext], Awaitable[RequestContext]]:
    """Dependency factory: authenticated context that holds every listed permission."""

    async def _dep(ctx: RequestContext = Depends(get_context)) -> RequestContext:
        ctx.principal.require(*permissions)
        return ctx

    return _dep
