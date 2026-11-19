"""Execution context handed to tool runners."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.database.session import tenant_session
from app.discovery.engine import load_credentials
from app.integrations.base import Target
from app.models import Integration, Resource, SshHostKey
from app.tools.base import ResourceInfo, ToolError
from app.tools.transport import Connection, Transport, get_transport


def resource_info(r: Resource) -> ResourceInfo:
    return ResourceInfo(
        id=r.id,
        name=r.name,
        resource_type=r.resource_type,
        hostname=r.hostname,
        ip_addresses=list(r.ip_addresses or []),
        integration_id=r.integration_id,
        discovery_target=r.discovery_target,
        facts=dict(r.facts or {}),
        health_status=r.health_status,
    )


def _target_for(integration: Integration, label: str | None) -> Target:
    for raw in integration.targets:
        with contextlib.suppress(KeyError, ValueError, TypeError):
            target = Target.from_dict(raw)
            if label is None or target.label == label:
                return target
    raise ToolError(
        "The resource's discovery target is no longer configured on its integration",
        code="no_target",
    )


async def resolve_connection(
    session: AsyncSession, settings: Settings, resource: ResourceInfo
) -> Connection:
    if resource.integration_id is None:
        raise ToolError(
            f"{resource.name} was added manually; there is no integration to reach it",
            code="no_integration",
        )
    integration = await session.get(Integration, resource.integration_id)
    if integration is None or not integration.enabled:
        raise ToolError("The resource's integration is missing or disabled", code="no_integration")
    config = dict(integration.configuration or {})
    target = _target_for(integration, resource.discovery_target)
    itype = integration.integration_type
    if itype == "windows_winrm":
        kind = "winrm"
    elif itype == "docker" and config.get("mode") == "tls":
        kind = "docker_tls"
    elif itype in ("linux_ssh", "docker"):
        kind = "ssh"
    else:
        raise ToolError(f"Integration type {itype} has no remote diagnostics", code="unsupported")
    container = None
    if resource.resource_type == "docker_container":
        container = str((resource.facts.get("container") or {}).get("name") or resource.name)
    pinned = None
    if kind == "ssh":
        port = target.port or int(config.get("port") or 22)
        pinned = await session.scalar(
            select(SshHostKey.public_key).where(
                SshHostKey.host == target.host, SshHostKey.port == port
            )
        )
    return Connection(
        kind=kind,
        target=target,
        config=config,
        credentials=load_credentials(settings, integration),
        pinned_key=pinned,
        container=container,
    )


@dataclass(slots=True)
class ToolContext:
    settings: Settings
    tenant_id: uuid.UUID
    resource: ResourceInfo | None
    timeout: float
    investigation_id: uuid.UUID | None = None
    _connection: Connection | None = None

    @contextlib.asynccontextmanager
    async def db(self) -> AsyncIterator[AsyncSession]:
        async with tenant_session(self.tenant_id) as session:
            yield session

    def require_resource(self) -> ResourceInfo:
        if self.resource is None:
            raise ToolError("This tool needs a resource_id", code="invalid_arguments")
        return self.resource

    async def connection(self) -> Connection:
        if self._connection is None:
            resource = self.require_resource()
            async with self.db() as session:
                self._connection = await resolve_connection(session, self.settings, resource)
        return self._connection

    async def transport(self) -> Transport:
        return get_transport(await self.connection(), self.timeout)

    async def container(self) -> str:
        conn = await self.connection()
        if not conn.container:
            raise ToolError("This resource is not a container", code="unsupported")
        return conn.container

    def facts(self) -> dict[str, Any]:
        return self.require_resource().facts
