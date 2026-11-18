"""Password hashing, JWT access tokens and opaque refresh tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings
from app.core.errors import AuthenticationError

_hasher = PasswordHasher()
_JWT_ALG = "HS256"
_ISSUER = "localops"
_AUDIENCE = "localops-api"

# Pre-computed hash used to equalise timing when a username doesn't exist.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


# --------------------------------------------------------------------------- JWT


@dataclass(frozen=True, slots=True)
class AccessClaims:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    jti: str
    issued_at: int
    expires_at: datetime


def create_access_token(
    settings: Settings, *, user_id: uuid.UUID, tenant_id: uuid.UUID
) -> tuple[str, AccessClaims]:
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=settings.access_token_ttl_minutes)
    jti = secrets.token_urlsafe(16)
    payload = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "sub": str(user_id),
        "tid": str(tenant_id),
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "typ": "access",
    }
    token = jwt.encode(payload, settings.app_secret_key.get_secret_value(), algorithm=_JWT_ALG)
    return token, AccessClaims(
        user_id=user_id,
        tenant_id=tenant_id,
        jti=jti,
        issued_at=int(now.timestamp()),
        expires_at=exp,
    )


def decode_access_token(settings: Settings, token: str) -> AccessClaims:
    try:
        payload = jwt.decode(
            token,
            settings.app_secret_key.get_secret_value(),
            algorithms=[_JWT_ALG],
            audience=_AUDIENCE,
            issuer=_ISSUER,
            options={"require": ["exp", "iat", "sub", "tid", "jti"]},
        )
        if payload.get("typ") != "access":
            raise AuthenticationError("Invalid token type")
        return AccessClaims(
            user_id=uuid.UUID(payload["sub"]),
            tenant_id=uuid.UUID(payload["tid"]),
            jti=str(payload["jti"]),
            issued_at=int(payload["iat"]),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Access token expired", code="token_expired") from exc
    except (jwt.InvalidTokenError, ValueError, KeyError) as exc:
        raise AuthenticationError("Invalid access token") from exc


# --------------------------------------------------------------------------- refresh tokens
#
# Format: "<tenant_id>.<token_id>.<secret>". Only SHA-256(secret) is stored. The tenant id
# lets the server establish the row-level-security context before looking the token up;
# tampering with it simply makes the lookup fail.


@dataclass(frozen=True, slots=True)
class RefreshTokenParts:
    tenant_id: uuid.UUID
    token_id: uuid.UUID
    secret: str


def new_refresh_token(tenant_id: uuid.UUID, token_id: uuid.UUID) -> tuple[str, str]:
    """Return ``(token, token_hash)``."""
    secret = secrets.token_urlsafe(32)
    return f"{tenant_id}.{token_id}.{secret}", hash_token_secret(secret)


def parse_refresh_token(token: str) -> RefreshTokenParts:
    try:
        tid, rid, secret = token.split(".", 2)
        if not secret:
            raise ValueError
        return RefreshTokenParts(uuid.UUID(tid), uuid.UUID(rid), secret)
    except ValueError as exc:
        raise AuthenticationError("Invalid refresh token") from exc


def hash_token_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def token_secret_matches(secret: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_token_secret(secret), token_hash)
