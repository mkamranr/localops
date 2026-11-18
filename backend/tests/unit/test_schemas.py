from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.identity import ChangePasswordRequest, LoginRequest, TenantCreate, UserCreate


def test_username_and_email_normalised() -> None:
    u = UserCreate(username="  Alice.Smith ", email="Alice@Example.COM", password="x" * 12)
    assert u.username == "alice.smith"
    assert u.email == "alice@example.com"


@pytest.mark.parametrize("username", ["ab", "has space", "semi;colon", "x" * 101])
def test_invalid_usernames(username: str) -> None:
    with pytest.raises(ValidationError):
        UserCreate(username=username, email="a@b.co", password="x" * 12)


def test_password_minimum_length() -> None:
    with pytest.raises(ValidationError):
        UserCreate(username="alice", email="a@b.co", password="short")


def test_change_password_must_differ() -> None:
    with pytest.raises(ValidationError):
        ChangePasswordRequest(current_password="same-password-1", new_password="same-password-1")


@pytest.mark.parametrize("slug", ["A", "-bad", "has_underscore", "UPPER"])
def test_invalid_tenant_slug(slug: str) -> None:
    admin: Any = {"username": "admin", "email": "a@b.co", "password": "x" * 12}
    with pytest.raises(ValidationError):
        TenantCreate(name="X", slug=slug, admin=admin)


def test_login_defaults_to_default_tenant() -> None:
    req = LoginRequest(username=" Admin ", password="p")
    assert req.tenant == "default"
    assert req.username == "admin"


@pytest.mark.parametrize("email", ["admin@localops.local", "ops@corp.internal", "A@B.LAN"])
def test_internal_domains_accepted(email: str) -> None:
    assert UserCreate(username="alice", email=email, password="x" * 12).email == email.lower()


@pytest.mark.parametrize("email", ["no-at-sign", "a@b", "a b@c.de", "@x.io"])
def test_bad_emails_rejected(email: str) -> None:
    with pytest.raises(ValidationError):
        UserCreate(username="alice", email=email, password="x" * 12)
