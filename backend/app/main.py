"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq.connections import ArqRedis
from fastapi import APIRouter, FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import ConnectionPool, Redis

from app.api.v1 import (
    ai,
    approvals,
    auth,
    incidents,
    integrations,
    investigations,
    knowledge,
    resources,
    system,
    tenants,
    tools,
    users,
)
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware
from app.core.redis import create_redis
from app.database.session import dispose_engine, init_engine
from app.graphs.dispatch import ArqDispatcher, InvestigationDispatcher

API_PREFIX = "/api/v1"


def create_app(
    settings: Settings | None = None,
    *,
    redis: Redis | None = None,
    dispatcher: InvestigationDispatcher | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json=settings.app_env != "development")
    log = get_logger("localops")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_engine(settings)
        app.state.redis = redis or create_redis(settings.redis_url)
        # arq needs a bytes-mode client; the pool connects lazily on first enqueue.
        app.state.dispatcher = dispatcher or ArqDispatcher(
            ArqRedis(connection_pool=ConnectionPool.from_url(settings.redis_url))
        )
        log.info("startup", env=settings.app_env)
        try:
            yield
        finally:
            if dispatcher is None:
                await app.state.dispatcher.close()
            if redis is None:
                await app.state.redis.aclose()
            await dispose_engine()
            log.info("shutdown")

    docs = not settings.is_production
    app = FastAPI(
        title="LocalOps API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=f"{API_PREFIX}/docs" if docs else None,
        redoc_url=None,
        openapi_url=f"{API_PREFIX}/openapi.json" if docs else None,
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    v1 = APIRouter(prefix=API_PREFIX)
    for module in (
        system,
        auth,
        tenants,
        users,
        ai,
        investigations,
        incidents,
        resources,
        integrations,
        tools,
        approvals,
        knowledge,
    ):
        v1.include_router(module.router)
    app.include_router(v1)

    # Internal-only: nginx proxies /api/ exclusively, so /metrics is reachable only
    # on the internal Docker network (for Prometheus).
    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory app.main:app_factory``."""
    return create_app()
