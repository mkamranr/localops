"""Shared test configuration.

Unit tests need nothing external. Integration tests (``-m integration``) need a
PostgreSQL database prepared with deployment/postgres/init/01-roles.sh and these env vars:
LOCALOPS_TEST_DB=1, DB_HOST, DB_PORT, DB_NAME, DB_OWNER_PASSWORD, DB_APP_PASSWORD.
Redis is replaced by fakeredis.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from collections.abc import AsyncIterator

import fakeredis
import httpx
import pytest
from fastapi import FastAPI

from app.core.config import get_settings
from app.main import create_app

INTEGRATION = os.environ.get("LOCALOPS_TEST_DB") == "1"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip = pytest.mark.skip(reason="integration tests need LOCALOPS_TEST_DB=1 and PostgreSQL")
    for item in items:
        if "integration" in item.nodeid.replace("\\", "/").split("/"):
            item.add_marker(pytest.mark.integration)
            if not INTEGRATION:
                item.add_marker(skip)


@pytest.fixture
async def redis() -> AsyncIterator[fakeredis.FakeAsyncRedis]:
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@pytest.fixture
async def app(redis: fakeredis.FakeAsyncRedis) -> AsyncIterator[FastAPI]:
    application = create_app(get_settings(), redis=redis)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
