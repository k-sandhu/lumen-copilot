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
"""

from __future__ import annotations

from typing import Protocol, cast
from uuid import UUID

import redis
import structlog

_log = structlog.get_logger(__name__)

# Redis key namespace for the fetch rate-limit counters (distinct from the
# Celery broker / WS backplane keyspaces sharing the same Redis).
_SOCKET_TIMEOUT_SECONDS = 2.0

_KEY_PREFIX = "lumen:ratelimit:source_sync"


class RateLimiter(Protocol):
    """A per-tenant fixed-window admission check (the seam tests fake).

    ``try_acquire`` records one fetch attempt for ``tenant_id`` and returns
    ``True`` when the tenant is still within its window budget (admit the sync),
    ``False`` when the window is exhausted (the caller must defer).
    """

    def try_acquire(self, tenant_id: UUID) -> bool: ...


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
    ) -> None:
        self._redis_url = redis_url
        self._max_per_window = max_per_window
        self._window_seconds = window_seconds
        # The Redis keyspace this limiter counts in — defaults to the connector-sync
        # namespace; other per-tenant limiters (run enqueue, web search) pass their
        # own distinct prefix so their windows do not collide (ADR-0015 §5).
        self._key_prefix = key_prefix

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


__all__ = ["RateLimiter", "RedisFixedWindowRateLimiter"]
