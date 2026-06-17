"""Redis backplane access — the only Redis client owner.

Per the ADR-0004 boundary table, ``realtime/`` owns Redis (pub/sub backplane for
fanning streamed output across instances). **Nobody else may publish/subscribe
Redis.** The full backplane (subscribe/publish for WS fan-out) lands with CC-6;
this skeleton exposes only :func:`ping`, the readiness reachability check, so the
Redis client stays behind this single module from day one.
"""

from __future__ import annotations

import redis.asyncio as aioredis


async def ping(redis_url: str) -> None:
    """Open a transient async client and ``PING`` Redis, then close it."""
    client = aioredis.from_url(redis_url)  # type: ignore[no-untyped-call]  # redis from_url is untyped
    try:
        await client.ping()
    finally:
        await client.aclose()
