"""Integration fixtures: migrated database, bootstrapped platform, helper clients."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config

from app.database.session import tenant_session
from app.services.tenant_service import ensure_platform_bootstrap

BACKEND = Path(__file__).resolve().parents[2]
ADMIN_PASSWORD = "Bootstrap-Admin-Pass-1"
STRONG = "Str0ng-Test-Password!"


def _alembic(*steps: tuple[str, str]) -> None:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.attributes["skip_logging_config"] = True
    for action, rev in steps:
        getattr(command, action)(cfg, rev)


@pytest.fixture(scope="session", autouse=True)
def migrated_db() -> None:
    """Fresh schema, exercising a full upgrade -> downgrade -> upgrade round-trip."""
    if os.environ.get("LOCALOPS_TEST_DB") != "1":
        return
    # env.py uses asyncio.run(); run it off the event-loop thread.
    with ThreadPoolExecutor(1) as pool:
        pool.submit(
            _alembic,
            ("downgrade", "base"),
            ("upgrade", "head"),
            ("downgrade", "base"),
            ("upgrade", "head"),
        ).result()


@pytest.fixture
async def bootstrapped(app: Any) -> None:
    async with tenant_session(None) as session:
        await ensure_platform_bootstrap(
            session,
            admin_username="admin",
            admin_email="admin@localops.local",
            admin_password=ADMIN_PASSWORD,
            create_default_tenant=True,
        )


@dataclass
class Session:
    client: httpx.AsyncClient
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.get(url, headers=self.headers, **kw)

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.post(url, headers=self.headers, **kw)

    async def put(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.put(url, headers=self.headers, **kw)

    async def delete(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.delete(url, headers=self.headers, **kw)


LoginFn = Callable[..., Coroutine[Any, Any, Session]]


@pytest.fixture
def login(app: Any) -> LoginFn:
    """Log in with a fresh client (own cookie jar) per call."""
    clients: list[httpx.AsyncClient] = []

    async def _login(tenant: str, username: str, password: str) -> Session:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        clients.append(c)
        r = await c.post(
            "/api/v1/auth/login",
            json={"tenant": tenant, "username": username, "password": password},
        )
        assert r.status_code == 200, r.text
        return Session(c, r.json()["access_token"])

    return _login


@pytest.fixture
async def platform_admin(bootstrapped: None, login: LoginFn) -> Session:
    return await login("system", "admin", ADMIN_PASSWORD)


@dataclass
class TenantInfo:
    id: str
    slug: str
    admin: Session


@pytest.fixture
def make_tenant(
    platform_admin: Session, login: LoginFn
) -> Callable[[], Coroutine[Any, Any, TenantInfo]]:
    async def _make() -> TenantInfo:
        slug = f"t-{uuid.uuid4().hex[:10]}"
        r = await platform_admin.post(
            "/api/v1/tenants",
            json={
                "name": f"Tenant {slug}",
                "slug": slug,
                "admin": {"username": "tadmin", "email": f"admin@{slug}.test", "password": STRONG},
            },
        )
        assert r.status_code == 201, r.text
        admin = await login(slug, "tadmin", STRONG)
        return TenantInfo(r.json()["id"], slug, admin)

    return _make


async def role_id(session: Session, name: str) -> str:
    roles = (await session.get("/api/v1/roles")).json()
    return next(r["id"] for r in roles if r["name"] == name)


@pytest.fixture
async def _cleanup_clients() -> AsyncIterator[None]:
    yield
