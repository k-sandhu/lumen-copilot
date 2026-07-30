"""Per-tenant fetch rate limit — Redis-backed fixed window (ADR-0009 §3, #20).

**Load-bearing (``risk:security``).** The server fetches *user-supplied* URLs, so
ADR-0009 §3 mandates a **per-tenant fetch rate limit** alongside the SSRF guard:
no single tenant may make the worker fan out unbounded outbound fetches (a
denial-of-service / amplification pivot). The limit is enforced at the
**sync-enqueue boundary** (``tasks.enqueue_source_sync``) — *not* at the HTTP
layer — so the frozen ``/sources`` contract needs no new status code (no 429): a
sync that would exceed the window is **deferred** (re-enqueued with backoff),
never returned as an undefined HTTP error.

The counter is a **fixed window** keyed per tenant: ``INCR`` a per-tenant,
per-window Redis key and ``EXPIRE`` it for the window length on first hit. The
first ``max_per_window`` syncs in a window are admitted; the rest are throttled.
Redis is the stack's existing shared store (the Celery broker / cache backplane),
so this adds no new infrastructure — the counter must be shared across worker +
API processes, which a per-process in-memory counter could not provide.

Fail-open on a Redis outage: if the counter store is unreachable the limiter
admits the sync (a transient Redis blip must not strand every tenant's syncs);
the SSRF guard at fetch time is the authoritative safety control, the rate limit
is an availability guard layered on top.

**Two entry points, one counter (#527).** The limiter began life on the Celery
sync-enqueue boundary, where blocking is free, so :meth:`try_acquire` opens a
blocking connection per call. Request-path callers arrived later — the
``web_search`` tool and MCP egress both admission-check inside a live chat turn —
and that shape parks the serving event loop on a TCP connect plus ``INCR`` for
every tool call. Those callers use :meth:`try_acquire_async`, which is
non-blocking and **pools** its connection per event loop. Both methods count in
the same keyspace with the same window, so a tenant's budget is shared however it
is spent.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, cast
from uuid import UUID

import redis
import redis.asyncio as aioredis
import structlog

_log = structlog.get_logger(__name__)

# Redis key namespace for the fetch rate-limit counters (distinct from the
# Celery broker / WS backplane keyspaces sharing the same Redis).
_SOCKET_TIMEOUT_SECONDS = 2.0

_KEY_PREFIX = "lumen:ratelimit:source_sync"

# Process-wide async clients, one per ``(redis_url, running loop)``.
#
# Deliberately module-level rather than per-instance: every caller builds a
# *fresh* limiter per request or per run (``build_web_search_service``,
# ``build_mcp_servers_service``), so a pool owned by the instance would still
# open a connection per search and the pooling would be worth nothing. The cache
# has to outlive the limiter for the connection to be reused at all.
#
# Keyed on the running loop for the reason the backplane is (#487): a client
# whose connections were opened on a now-dead loop must never be handed out —
# the #140 "attached to a different loop" failure. A stale entry is dropped, not
# closed; its sockets died with its loop, and awaiting its close from *this* loop
# would touch a foreign one.
_ASYNC_POOLS: dict[str, tuple[Any, asyncio.AbstractEventLoop]] = {}


async def aclose_async_rate_limit_pools() -> None:
    """Release every pooled async client (app lifespan shutdown)."""
    pools = list(_ASYNC_POOLS.values())
    _ASYNC_POOLS.clear()
    for client, _loop in pools:
        try:
            await client.aclose()
        except Exception:  # pragma: no cover - shutdown must not raise
            _log.warning("rate_limit.pool_close_failed")


class RateLimiter(Protocol):
    """A per-tenant fixed-window admission check (the seam tests fake).

    ``try_acquire`` records one fetch attempt for ``tenant_id`` and returns
    ``True`` when the tenant is still within its window budget (admit the sync),
    ``False`` when the window is exhausted (the caller must defer).
    """

    def try_acquire(self, tenant_id: UUID) -> bool: ...


class AsyncRateLimiter(Protocol):
    """The same admission check for callers on an event loop (#527).

    Separate from :class:`RateLimiter` rather than an added method on it, so the
    Celery callers and their fakes keep a purely synchronous seam and only the
    request-path services depend on the awaitable one.
    """

    async def try_acquire_async(self, tenant_id: UUID) -> bool: ...


class RedisFixedWindowRateLimiter:
    """Redis-backed fixed-window per-tenant fetch rate limiter (ADR-0009 §3).

    One counter key per ``(tenant, window)``; ``INCR`` is atomic so concurrent
    workers share one true count. The key is created with ``EXPIRE`` on its first
    increment so windows roll forward without a sweeper. Constructed from the
    Redis URL (the stack's existing store) and the window/limit from settings.
    """

    def __init__(
        self,
        redis_url: str,
        *,
        max_per_window: int,
        window_seconds: int,
        key_prefix: str = _KEY_PREFIX,
        async_client_factory: Any | None = None,
    ) -> None:
        self._redis_url = redis_url
        self._max_per_window = max_per_window
        self._window_seconds = window_seconds
        # The Redis keyspace this limiter counts in — defaults to the connector-sync
        # namespace; other per-tenant limiters (run enqueue, web search) pass their
        # own distinct prefix so their windows do not collide (ADR-0015 §5).
        self._key_prefix = key_prefix
        # The pooled async client and the loop it was built on. ``None`` until the
        # first async call, so construction stays I/O-free and loop-free and the
        # limiter can still be built at import/config time.
        self._async_client_factory = async_client_factory
        self._pool: Any | None = None
        self._pool_loop: asyncio.AbstractEventLoop | None = None

    def _new_async_client(self) -> Any:
        if self._async_client_factory is not None:
            return self._async_client_factory()
        return aioredis.from_url(  # type: ignore[no-untyped-call]
            self._redis_url,
            socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
            socket_timeout=_SOCKET_TIMEOUT_SECONDS,
        )

    def _async_client(self) -> Any:
        """The pooled async client for the RUNNING loop, created on first use.

        An **injected** factory (tests) stays per-instance so cases cannot leak
        clients into each other. The real client comes from the process-wide
        :data:`_ASYNC_POOLS`, because production builds a fresh limiter per
        request — see that cache's note.
        """
        loop = asyncio.get_running_loop()
        if self._async_client_factory is not None:
            if self._pool is None or self._pool_loop is not loop:
                self._pool = self._async_client_factory()
                self._pool_loop = loop
            return self._pool

        cached = _ASYNC_POOLS.get(self._redis_url)
        if cached is not None and cached[1] is loop:
            return cached[0]
        client = self._new_async_client()
        _ASYNC_POOLS[self._redis_url] = (client, loop)
        return client

    async def try_acquire_async(self, tenant_id: UUID) -> bool:
        """:meth:`try_acquire` for callers on an event loop (#527).

        Identical counter, keyspace, window and fail-open behaviour — the only
        difference is that it neither blocks the loop nor opens a fresh
        connection per call. Request-path callers (``web_search``, MCP egress)
        must use this; the blocking form would park the whole worker on a TCP
        connect plus ``INCR`` for every tool invocation.
        """
        key = f"{self._key_prefix}:{tenant_id}"
        try:
            client = self._async_client()
            count = int(await client.incr(key))
            if count == 1:
                # First hit in this window — arm the expiry so the window rolls.
                await client.expire(key, self._window_seconds)
        except redis.RedisError as exc:
            # Same fail-open as the sync path: a transient counter-store outage
            # must not strand callers; the SSRF guard stays authoritative.
            _log.warning(
                "source_sync.rate_limit_unavailable",
                tenant_id=str(tenant_id),
                error=type(exc).__name__,
            )
            # Retire the pooled client so the next call rebuilds rather than
            # reusing a connection that just failed.
            self._pool = None
            self._pool_loop = None
            _ASYNC_POOLS.pop(self._redis_url, None)
            return True
        return count <= self._max_per_window

    async def aclose(self) -> None:
        """Release this instance's injected client, if it built one (tests).

        The production client lives in the process-wide pool; the app lifespan
        releases that through :func:`aclose_async_rate_limit_pools`, since no one
        limiter instance owns it.
        """
        pool, self._pool, self._pool_loop = self._pool, None, None
        if pool is not None:
            await pool.aclose()

    def try_acquire(self, tenant_id: UUID) -> bool:
        """Admit (``True``) or throttle (``False``) one fetch for ``tenant_id``.

        Increments the tenant's current-window counter and sets the window TTL on
        the first hit. Admits while the count is at or below ``max_per_window``.
        Fail-open: a Redis error admits the sync (availability over strictness;
        the SSRF guard remains the authoritative control).
        """
        key = f"{self._key_prefix}:{tenant_id}"
        try:
            client: redis.Redis = redis.Redis.from_url(
                self._redis_url,
                # #271: bound the blocking socket I/O — a Redis blip fails fast
                # (and fail-open below) instead of hanging the calling thread.
                socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
                socket_timeout=_SOCKET_TIMEOUT_SECONDS,
            )
            try:
                # The sync client's stubs share the async surface (return type is a
                # union including Awaitable); on the sync client these are concrete
                # values, so narrow the count explicitly.
                count = int(cast(int, client.incr(key)))
                if count == 1:
                    # First hit in this window — arm the expiry so the window rolls.
                    client.expire(key, self._window_seconds)
            finally:
                client.close()  # type: ignore[no-untyped-call]  # redis sync close is untyped
        except redis.RedisError as exc:
            # A transient counter-store outage must not strand syncs; admit and
            # let the fetch-time SSRF guard remain the authoritative control.
            _log.warning(
                "source_sync.rate_limit_unavailable",
                tenant_id=str(tenant_id),
                error=type(exc).__name__,
            )
            return True
        return count <= self._max_per_window


__all__ = [
    "AsyncRateLimiter",
    "RateLimiter",
    "RedisFixedWindowRateLimiter",
    "aclose_async_rate_limit_pools",
]
