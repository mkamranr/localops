import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.errors import AuthenticationError
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    hash_token_secret,
    new_refresh_token,
    parse_refresh_token,
    token_secret_matches,
    verify_password,
)


def test_password_hash_roundtrip() -> None:
    h = hash_password("correct horse battery")
    assert h.startswith("$argon2")
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong", h)
    assert not verify_password("anything", None)
    assert not verify_password("anything", "not-a-hash")


def test_access_token_roundtrip() -> None:
    s = get_settings()
    uid, tid = uuid.uuid4(), uuid.uuid4()
    token, claims = create_access_token(s, user_id=uid, tenant_id=tid)
    decoded = decode_access_token(s, token)
    assert decoded.user_id == uid
    assert decoded.tenant_id == tid
    assert decoded.jti == claims.jti
    assert decoded.issued_at == claims.issued_at


def _forge(payload: dict[str, object], key: str | None = None) -> str:
    s = get_settings()
    return jwt.encode(payload, key or s.app_secret_key.get_secret_value(), algorithm="HS256")


def _base_payload(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "iss": "localops",
        "aud": "localops-api",
        "sub": str(uuid.uuid4()),
        "tid": str(uuid.uuid4()),
        "jti": "abc",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "typ": "access",
    }
    payload.update(overrides)
    return payload


def test_expired_token_rejected() -> None:
    past = int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())
    with pytest.raises(AuthenticationError) as exc:
        decode_access_token(get_settings(), _forge(_base_payload(exp=past)))
    assert exc.value.code == "token_expired"


@pytest.mark.parametrize(
    "payload",
    [
        _base_payload(typ="refresh"),
        _base_payload(aud="someone-else"),
        _base_payload(iss="evil"),
        {k: v for k, v in _base_payload().items() if k != "tid"},
    ],
)
def test_invalid_claims_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(AuthenticationError):
        decode_access_token(get_settings(), _forge(payload))


def test_wrong_signature_rejected() -> None:
    with pytest.raises(AuthenticationError):
        decode_access_token(get_settings(), _forge(_base_payload(), key="x" * 40))


def test_none_algorithm_rejected() -> None:
    token = jwt.encode(_base_payload(), key="", algorithm="none")
    with pytest.raises(AuthenticationError):
        decode_access_token(get_settings(), token)


def test_refresh_token_format() -> None:
    tid, rid = uuid.uuid4(), uuid.uuid4()
    token, token_hash = new_refresh_token(tid, rid)
    parts = parse_refresh_token(token)
    assert (parts.tenant_id, parts.token_id) == (tid, rid)
    assert token_secret_matches(parts.secret, token_hash)
    assert token_hash == hash_token_secret(parts.secret)
    assert not token_secret_matches(parts.secret + "x", token_hash)


@pytest.mark.parametrize("bad", ["", "abc", "a.b.c", f"{uuid.uuid4()}.{uuid.uuid4()}."])
def test_refresh_token_parse_rejects_garbage(bad: str) -> None:
    with pytest.raises(AuthenticationError):
        parse_refresh_token(bad)
