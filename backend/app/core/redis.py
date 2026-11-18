"""Redis client and tenant-namespaced key helpers.

All tenant-related keys are namespaced ``localops:t:<tenant_id>:...`` (spec §13.4).
"""

from __future__ import annotations

import uuid

from redis.asyncio import Redis

PREFIX = "localops"


def create_redis(url: str) -> Redis:
    client: Redis = Redis.from_url(
        url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5
    )
    return client


def tenant_key(tenant_id: uuid.UUID, *parts: str) -> str:
    return ":".join((PREFIX, "t", str(tenant_id), *parts))


def global_key(*parts: str) -> str:
    return ":".join((PREFIX, *parts))
