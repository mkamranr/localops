"""Request-scoped identity and client information passed into services."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PermissionDeniedError


@dataclass(frozen=True, slots=True)
class ClientInfo:
    ip_address: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID
    username: str
    tenant_id: uuid.UUID
    tenant_slug: str
    permissions: frozenset[str]
    roles: tuple[str, ...] = ()
    jti: str = ""
    token_expires_at: datetime | None = None

    @property
    def is_platform_admin(self) -> bool:
        return "tenant.manage" in self.permissions

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def require(self, *permissions: str) -> None:
        missing = [p for p in permissions if p not in self.permissions]
        if missing:
            raise PermissionDeniedError(
                "You do not have permission to perform this action",
                details={"missing_permissions": missing},
            )


@dataclass(slots=True)
class RequestContext:
    """Authenticated unit of work: principal + tenant-bound DB session."""

    session: AsyncSession
    principal: Principal
    client: ClientInfo = field(default_factory=ClientInfo)
