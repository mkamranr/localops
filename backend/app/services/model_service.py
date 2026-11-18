"""Model endpoint registry (spec §14): CRUD, default selection, connectivity tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.database.session import tenant_session
from app.llm.gateway import EndpointConfig, ModelClient
from app.models import Agent, ModelEndpoint
from app.schemas.ai import (
    ModelEndpointCreate,
    ModelEndpointOut,
    ModelEndpointUpdate,
    ModelTestResult,
)
from app.services import audit_service as audit
from app.services.context import ClientInfo, Principal, RequestContext

log = get_logger("localops.models")


def to_out(m: ModelEndpoint) -> ModelEndpointOut:
    out = ModelEndpointOut.model_validate(m)
    out.has_api_key = bool(m.encrypted_api_key)
    return out


async def list_models(session: AsyncSession) -> list[ModelEndpoint]:
    rows = await session.scalars(
        select(ModelEndpoint).order_by(ModelEndpoint.is_default.desc(), ModelEndpoint.name)
    )
    return list(rows)


async def get_model(session: AsyncSession, model_id: uuid.UUID) -> ModelEndpoint:
    model = await session.get(ModelEndpoint, model_id)
    if model is None:
        raise NotFoundError("Model not found")
    return model


async def _name_taken(session: AsyncSession, name: str, exclude: uuid.UUID | None = None) -> bool:
    q = (
        select(func.count())
        .select_from(ModelEndpoint)
        .where(func.lower(ModelEndpoint.name) == name.lower())
    )
    if exclude:
        q = q.where(ModelEndpoint.id != exclude)
    return bool(await session.scalar(q))


async def _clear_default(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    await session.execute(
        update(ModelEndpoint)
        .where(ModelEndpoint.tenant_id == tenant_id, ModelEndpoint.is_default.is_(True))
        .values(is_default=False)
    )


async def create_model(
    ctx: RequestContext, settings: Settings, data: ModelEndpointCreate
) -> ModelEndpoint:
    s, p = ctx.session, ctx.principal
    if await _name_taken(s, data.name):
        raise ConflictError("A model with this name already exists", details={"field": "name"})
    has_any = bool(await s.scalar(select(func.count()).select_from(ModelEndpoint)))
    make_default = data.is_default or not has_any  # first model becomes the default
    if make_default:
        await _clear_default(s, p.tenant_id)
    model = ModelEndpoint(
        tenant_id=p.tenant_id,
        name=data.name.strip(),
        provider=data.provider,
        base_url=data.base_url,
        model_name=data.model_name.strip(),
        description=data.description,
        configuration=data.configuration.model_dump(exclude_none=True),
        encrypted_api_key=(
            encrypt_secret(settings, data.api_key.get_secret_value())
            if data.api_key and data.api_key.get_secret_value()
            else None
        ),
        enabled=data.enabled,
        is_default=make_default and data.enabled,
    )
    s.add(model)
    await s.flush()
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="model.create",
        entity_type="model",
        entity_id=model.id,
        details={
            "name": model.name,
            "provider": model.provider,
            "model": model.model_name,
            "base_url": model.base_url,
            "default": model.is_default,
        },
        client=ctx.client,
    )
    return model


async def update_model(
    ctx: RequestContext, settings: Settings, model_id: uuid.UUID, data: ModelEndpointUpdate
) -> ModelEndpoint:
    s, p = ctx.session, ctx.principal
    model = await get_model(s, model_id)
    changes: dict[str, object] = {}
    if data.name is not None and data.name != model.name:
        if await _name_taken(s, data.name, exclude=model.id):
            raise ConflictError("A model with this name already exists", details={"field": "name"})
        model.name = changes["name"] = data.name.strip()
    for field in ("provider", "base_url", "model_name", "description", "enabled"):
        value = getattr(data, field)
        if value is not None and value != getattr(model, field):
            setattr(model, field, value)
            changes[field] = value
    if data.configuration is not None:
        model.configuration = data.configuration.model_dump(exclude_none=True)
        changes["configuration"] = model.configuration
    if data.clear_api_key and model.encrypted_api_key:
        model.encrypted_api_key = None
        changes["api_key"] = "cleared"
    elif data.api_key is not None and data.api_key.get_secret_value():
        model.encrypted_api_key = encrypt_secret(settings, data.api_key.get_secret_value())
        changes["api_key"] = "replaced"
    if data.is_default is True and not model.is_default:
        await _clear_default(s, p.tenant_id)
        await s.refresh(model)
        model.is_default = True
        changes["is_default"] = True
    elif data.is_default is False and model.is_default:
        model.is_default = False
        changes["is_default"] = False
    if not model.enabled and model.is_default:
        model.is_default = False
        changes["is_default"] = False
    if any(k in changes for k in ("provider", "base_url", "model_name", "api_key")):
        model.last_status = None
        model.last_error = None
        model.last_checked_at = None
        model.last_latency_ms = None
    await s.flush()
    if changes:
        await audit.record(
            s,
            tenant_id=p.tenant_id,
            actor=p,
            action="model.update",
            entity_type="model",
            entity_id=model.id,
            details={"name": model.name, "changes": changes},
            client=ctx.client,
        )
    return model


async def delete_model(ctx: RequestContext, model_id: uuid.UUID) -> None:
    s, p = ctx.session, ctx.principal
    model = await get_model(s, model_id)
    # Agents pinned to this model fall back to the tenant default.
    await s.execute(update(Agent).where(Agent.model_id == model.id).values(model_id=None))
    await s.delete(model)
    await s.flush()
    await audit.record(
        s,
        tenant_id=p.tenant_id,
        actor=p,
        action="model.delete",
        entity_type="model",
        entity_id=model_id,
        details={"name": model.name},
        client=ctx.client,
    )


def endpoint_config(settings: Settings, model: ModelEndpoint) -> EndpointConfig:
    return EndpointConfig.from_record(
        provider=model.provider,
        base_url=model.base_url,
        model_name=model.model_name,
        name=model.name,
        configuration=model.configuration,
        api_key=decrypt_secret(settings, model.encrypted_api_key)
        if model.encrypted_api_key
        else None,
    )


async def test_model(
    principal: Principal, client: ClientInfo, settings: Settings, model_id: uuid.UUID
) -> ModelTestResult:
    """Probe the endpoint (model list + tiny completion) and store the outcome.

    Uses two short transactions so no DB connection is held during the probe.
    """
    async with tenant_session(principal.tenant_id) as s:
        model = await get_model(s, model_id)
        config = endpoint_config(settings, model)
        model_name = model.model_name
    health = await ModelClient(config).health()
    status = "healthy" if health.ok else "unhealthy"
    error = (health.error or "")[:500] or None
    if health.ok and health.model_available is False:
        status = "degraded"
        error = f"Model '{model_name}' is not listed by the endpoint"
    async with tenant_session(principal.tenant_id) as s:
        model = await get_model(s, model_id)
        model.last_checked_at = datetime.now(UTC)
        model.last_status = status
        model.last_latency_ms = health.latency_ms
        model.last_error = error
        await s.flush()
        await audit.record(
            s,
            tenant_id=principal.tenant_id,
            actor=principal,
            action="model.test",
            outcome="success" if health.ok else "failure",
            entity_type="model",
            entity_id=model.id,
            details={"name": model.name, "status": status, "latency_ms": health.latency_ms},
            client=client,
        )
    return ModelTestResult(
        ok=health.ok,
        latency_ms=health.latency_ms,
        models=health.models[:100],
        model_available=health.model_available,
        reply=health.reply,
        error=error,
    )


async def ensure_env_default_model(
    session: AsyncSession, settings: Settings, tenant_id: uuid.UUID
) -> bool:
    """Seed a default endpoint from ``LLM_*`` settings if the tenant has none."""
    if not settings.llm_base_url or not settings.llm_model:
        return False
    if await session.scalar(select(func.count()).select_from(ModelEndpoint)):
        return False
    session.add(
        ModelEndpoint(
            tenant_id=tenant_id,
            name="Default model",
            provider=settings.llm_provider,
            base_url=settings.llm_base_url.rstrip("/"),
            model_name=settings.llm_model,
            description="Created from the LLM_* environment settings at bootstrap.",
            configuration={"timeout_seconds": 120, "max_retries": 2},
            enabled=True,
            is_default=True,
        )
    )
    await session.flush()
    return True
