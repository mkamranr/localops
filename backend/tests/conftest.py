"""Shared test configuration.

Unit tests need nothing external. Integration tests (``-m integration``) need a
PostgreSQL database prepared with deployment/postgres/init/01-roles.sh and these env vars:
LOCALOPS_TEST_DB=1, DB_HOST, DB_PORT, DB_NAME, DB_OWNER_PASSWORD, DB_APP_PASSWORD.
Redis is replaced by fakeredis.
"""

from __future__ import annotations

import asyncio
import os
import sys

# psycopg's async mode (LangGraph checkpointer) cannot use Windows' default Proactor
# loop. Production runs on Linux; this only matters for local Windows test runs.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined,unused-ignore]

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")
# Test-only Fernet key.
os.environ.setdefault("LOCALOPS_ENCRYPTION_KEY", "kJ0m3Qx3l5n9oA0m3cR2nS4pZ7Yw1vB6tU8xE5gH2dQ=")

from collections.abc import AsyncIterator, Iterator

import fakeredis
import httpx
import pytest
from fastapi import FastAPI

from app.core.config import get_settings
from app.graphs.dispatch import InlineDispatcher
from app.llm import gateway
from app.main import create_app
from tests.fakes import FakeLLM

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
def fake_llm() -> Iterator[FakeLLM]:
    """Route every model call to an in-process fake OpenAI-compatible server."""
    fake = FakeLLM()
    gateway.set_transport_override(fake.transport())
    yield fake
    gateway.set_transport_override(None)


@pytest.fixture
async def app(redis: fakeredis.FakeAsyncRedis) -> AsyncIterator[FastAPI]:
    settings = get_settings()
    application = create_app(settings, redis=redis, dispatcher=InlineDispatcher(settings, redis))
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
