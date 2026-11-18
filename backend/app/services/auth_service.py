"""Local authentication: login, refresh-token rotation, logout, password change."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, AuthenticationError, RateLimitedError
from app.core.logging import get_logger
from app.core.redis import global_key, tenant_key
from app.core.security import (
    create_access_token,
    hash_password,
    new_refresh_token,
    parse_refresh_token,
    password_needs_rehash,
    token_secret_matches,
    verify_password,
)
from app.database.session import set_tenant_context, tenant_session
from app.models import RefreshToken, User
from app.repositories import identity as repo
from app.services import audit_service as audit
from app.services.context import ClientInfo, RequestContext

log = get_logger(__name__)

INVALID_CREDENTIALS = "Invalid tenant, username or password"
# A rotated refresh token presented again within this window is treated as a benign
# race (e.g. two browser tabs refreshing at once) rather than token theft.
REFRESH_REUSE_GRACE = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_at: datetime


# --------------------------------------------------------------------------- helpers


async def _issue_tokens(
    session: AsyncSession,
    settings: Settings,
    user: User,
    *,
    family_id: uuid.UUID | None = None,
    user_agent: str | None = None,
) -> tuple[IssuedTokens, RefreshToken]:
    token_id = uuid.uuid4()
    refresh, token_hash = new_refresh_token(user.tenant_id, token_id)
    expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days)
    row = RefreshToken(
        id=token_id,
        tenant_id=user.tenant_id,
        user_id=user.id,
        family_id=family_id or uuid.uuid4(),
        token_hash=token_hash,
        expires_at=expires_at,
        user_agent=(user_agent or "")[:500] or None,
    )
    session.add(row)
    await session.flush()
    access, _ = create_access_token(settings, user_id=user.id, tenant_id=user.tenant_id)
    return (
        IssuedTokens(
            access_token=access,
            expires_in=settings.access_token_ttl_minutes * 60,
            refresh_token=refresh,
            refresh_expires_at=expires_at,
        ),
        row,
    )


async def _check_rate_limit(redis: Redis, settings: Settings, key: str) -> None:
    attempts = await redis.incr(key)
    if attempts == 1:
        await redis.expire(key, settings.login_rate_limit_window_seconds)
    if attempts > settings.login_rate_limit_attempts:
        raise RateLimitedError("Too many login attempts. Please try again later.")


async def deny_access_token(
    redis: Redis, tenant_id: uuid.UUID, jti: str, expires_at: datetime | None
) -> None:
    ttl = int((expires_at - datetime.now(UTC)).total_seconds()) if expires_at else 3600
    if ttl > 0:
        await redis.set(tenant_key(tenant_id, "jti_denylist", jti), "1", ex=ttl)


async def is_access_token_denied(redis: Redis, tenant_id: uuid.UUID, jti: str) -> bool:
    return bool(await redis.exists(tenant_key(tenant_id, "jti_denylist", jti)))


# --------------------------------------------------------------------------- login


async def login(
    settings: Settings,
    redis: Redis,
    *,
    tenant_slug: str,
    username: str,
    password: str,
    client: ClientInfo,
) -> IssuedTokens:
    rl_key = global_key("ratelimit", "login", tenant_slug, username, client.ip_address or "-")
    await _check_rate_limit(redis, settings, rl_key)

    issued: IssuedTokens | None = None
    async with tenant_session(None) as session:
        resolved = await repo.resolve_tenant_slug(session, tenant_slug)
        if resolved is None:
            verify_password(password, None)  # equalise timing
            log.info("login_failed", reason="unknown_tenant", tenant=tenant_slug)
        else:
            tenant_id, tenant_status = resolved
            await set_tenant_context(session, tenant_id)
            user = await repo.get_user_by_username(session, tenant_id, username)
            password_ok = verify_password(password, user.password_hash if user else None)
            reason = None
            if user is None or not password_ok:
                reason = "invalid_credentials"
            elif user.status != "active":
                reason = "user_disabled"
            elif tenant_status != "active":
                reason = "tenant_disabled"

            if reason or user is None:
                await audit.record(
                    session,
                    tenant_id=tenant_id,
                    action="auth.login",
                    outcome="failure",
                    user_id=user.id if user else None,
                    username=username,
                    details={"reason": reason},
                    client=client,
                )
            else:
                user.last_login_at = datetime.now(UTC)
                if password_needs_rehash(user.password_hash):
                    user.password_hash = hash_password(password)
                issued, _ = await _issue_tokens(
                    session, settings, user, user_agent=client.user_agent
                )
                await audit.record(
                    session,
                    tenant_id=tenant_id,
                    action="auth.login",
                    user_id=user.id,
                    username=user.username,
                    client=client,
                )

    if issued is None:
        raise AuthenticationError(INVALID_CREDENTIALS, code="invalid_credentials")
    await redis.delete(rl_key)
    return issued


# --------------------------------------------------------------------------- refresh


async def refresh(
    settings: Settings, redis: Redis, *, refresh_token: str, client: ClientInfo
) -> IssuedTokens:
    parts = parse_refresh_token(refresh_token)
    issued: IssuedTokens | None = None
    failure = "Invalid refresh token"

    async with tenant_session(parts.tenant_id) as session:
        row = (
            await session.execute(
                select(RefreshToken).where(RefreshToken.id == parts.token_id).with_for_update()
            )
        ).scalar_one_or_none()
        now = datetime.now(UTC)
        if row is None or not token_secret_matches(parts.secret, row.token_hash):
            pass
        elif row.revoked_at is not None and not (
            row.replaced_by is not None and now - row.revoked_at <= REFRESH_REUSE_GRACE
        ):
            # Reuse of a rotated/revoked token: assume theft and kill the whole family.
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            await audit.record(
                session,
                tenant_id=row.tenant_id,
                action="auth.refresh_reuse",
                outcome="failure",
                user_id=row.user_id,
                details={"family_id": str(row.family_id)},
                client=client,
            )
            failure = "Refresh token has been revoked"
        elif row.expires_at <= now:
            failure = "Refresh token expired"
        else:
            user = await repo.get_user(session, row.tenant_id, row.user_id)
            tenant = await repo.get_tenant(session, row.tenant_id)
            if user and user.status == "active" and tenant and tenant.status == "active":
                issued, new_row = await _issue_tokens(
                    session, settings, user, family_id=row.family_id, user_agent=client.user_agent
                )
                if row.revoked_at is None:
                    row.revoked_at = now
                    row.replaced_by = new_row.id

    if issued is None:
        raise AuthenticationError(failure, code="invalid_refresh_token")
    return issued


# --------------------------------------------------------------------------- logout


async def logout(ctx: RequestContext, redis: Redis, *, refresh_token: str | None) -> None:
    p = ctx.principal
    if refresh_token:
        try:
            parts = parse_refresh_token(refresh_token)
        except AuthenticationError:
            parts = None
        if parts and parts.tenant_id == p.tenant_id:
            row = await ctx.session.get(RefreshToken, parts.token_id)
            if (
                row
                and row.user_id == p.user_id
                and token_secret_matches(parts.secret, row.token_hash)
            ):
                await ctx.session.execute(
                    update(RefreshToken)
                    .where(
                        RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None)
                    )
                    .values(revoked_at=datetime.now(UTC))
                )
    await deny_access_token(redis, p.tenant_id, p.jti, p.token_expires_at)
    await audit.record(
        ctx.session, tenant_id=p.tenant_id, actor=p, action="auth.logout", client=ctx.client
    )


# --------------------------------------------------------------------------- password


async def change_password(
    ctx: RequestContext,
    settings: Settings,
    redis: Redis,
    *,
    current_password: str,
    new_password: str,
) -> IssuedTokens:
    p = ctx.principal
    user = await repo.get_user(ctx.session, p.tenant_id, p.user_id)
    if user is None:
        raise AuthenticationError("User not found")
    if not verify_password(current_password, user.password_hash):
        # Record the failure in its own transaction: raising rolls back ctx.session.
        async with tenant_session(p.tenant_id) as audit_session:
            await audit.record(
                audit_session,
                tenant_id=p.tenant_id,
                actor=p,
                action="auth.password_change",
                outcome="failure",
                details={"reason": "wrong_current_password"},
                client=ctx.client,
            )
        raise AuthenticationError("Current password is incorrect", code="invalid_credentials")
    if new_password.lower() == user.username.lower():
        raise AppError(
            "Password must not equal the username", code="weak_password", status_code=422
        )

    now = datetime.now(UTC)
    user.password_hash = hash_password(new_password)
    user.password_changed_at = now
    user.must_change_password = False
    await ctx.session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await deny_access_token(redis, p.tenant_id, p.jti, p.token_expires_at)
    issued, _ = await _issue_tokens(ctx.session, settings, user, user_agent=ctx.client.user_agent)
    await audit.record(
        ctx.session,
        tenant_id=p.tenant_id,
        actor=p,
        action="auth.password_change",
        client=ctx.client,
    )
    return issued
