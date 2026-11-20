"""Per-tenant tool catalogue: built-in tools plus enabled, approved tenant tools.

Built-in tools are code (``app.tools.registry``); MCP and HTTP-template tools are rows
in ``tool_definitions``. Loading is RLS-scoped, so a tenant only ever sees its own
tools. A short in-process cache avoids a query per tool call; dynamic runners
re-check the definition when they execute, so staleness never re-enables a tool.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import tenant_session
from app.models import ToolDefinition
from app.tools.base import ToolSpec
from app.tools.dynamic import spec_from_definition
from app.tools.registry import AGENT_TOOLS, TOOLS

CACHE_SECONDS = 30.0
_cache: dict[uuid.UUID, tuple[float, Catalogue]] = {}


@dataclass(frozen=True, slots=True)
class Catalogue:
    tools: dict[str, ToolSpec]

    def get(self, name: str) -> ToolSpec | None:
        return self.tools.get(name)

    def read_tools(self) -> list[ToolSpec]:
        return [t for t in self.tools.values() if not t.is_change]

    def change_tools(self) -> list[ToolSpec]:
        return [t for t in self.tools.values() if t.is_change]

    def for_agent(self, agent_type: str, allowed: list[str] | None = None) -> list[ToolSpec]:
        """Read tools for an agent; change tools are never handed to an agent."""
        names = list(allowed) if allowed else list(AGENT_TOOLS.get(agent_type, ()))
        out = []
        for name in dict.fromkeys(names):
            spec = self.tools.get(name)
            if spec is not None and not spec.is_change:
                out.append(spec)
        return out


BUILTIN = Catalogue(dict(TOOLS))


async def load(session: AsyncSession) -> Catalogue:
    """Built-in + the session tenant's usable tools (no cache)."""
    rows = await session.scalars(
        select(ToolDefinition).where(
            ToolDefinition.enabled.is_(True), ToolDefinition.status == "active"
        )
    )
    tools = dict(TOOLS)
    for row in rows:
        if row.approved_schema_hash != row.schema_hash or row.name in TOOLS:
            continue
        spec = spec_from_definition(row)
        if spec is not None:
            tools[spec.name] = spec
    return Catalogue(tools)


async def for_tenant(tenant_id: uuid.UUID) -> Catalogue:
    hit = _cache.get(tenant_id)
    now = time.monotonic()
    if hit is not None and hit[0] > now:
        return hit[1]
    async with tenant_session(tenant_id) as session:
        catalogue = await load(session)
    _cache[tenant_id] = (now + CACHE_SECONDS, catalogue)
    return catalogue


def invalidate(tenant_id: uuid.UUID | None = None) -> None:
    if tenant_id is None:
        _cache.clear()
    else:
        _cache.pop(tenant_id, None)
