"""Atomic per-tenant sandbox accounting (#589, ADR-0029 proposed).

Two counters, both tenant-scoped and both decided by ONE Redis Lua
evaluation so concurrent admissions cannot both win the last slot — the
check-then-act ``count_executing()`` gate this replaces admitted both:

* a **concurrency lease** — a sorted set of run ids scored by lease expiry.
  Acquiring prunes expired leases and adds the run iff the tenant is below
  its cap. The TTL (wall clock + grace) reaps leases whose holder died;
  a live holder renews with its ownership token and releases after confirmation.
* a **daily runtime counter** — admission reserves the run's wall-clock
  budget against the tenant's UTC-day cap; completion settles the
  difference between reserved and actually-measured seconds.

**Fail closed.** Unlike the fetch rate limiter (an availability guard),
these counters are the enforcement mechanism for a resource-isolation
control: if Redis cannot answer, admission raises
:class:`AccountingUnavailable` and the run is refused. Release/settle are
best-effort (a missed release is healed by the lease TTL; a missed settle
only over-counts, which fails toward refusing).
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

import redis
import redis.asyncio as aioredis
import structlog

_log = structlog.get_logger(__name__)

_SOCKET_TIMEOUT_SECONDS = 2.0
_LEASE_PREFIX = "lumen:sandbox:conc"
_DAILY_PREFIX = "lumen:sandbox:daily"

_ACQUIRE_SCRIPT = """
-- LEASE_ACQUIRE
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[4])
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then return 2 end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[1])
redis.call('HSET', KEYS[2], ARGV[1], ARGV[6])
redis.call('EXPIRE', KEYS[1], math.max(tonumber(ARGV[5]), redis.call('TTL', KEYS[1])))
redis.call('EXPIRE', KEYS[2], math.max(tonumber(ARGV[5]), redis.call('TTL', KEYS[2])))
return 1
"""

_RENEW_SCRIPT = """
-- LEASE_RENEW
local expiry = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not expiry or tonumber(expiry) <= tonumber(ARGV[3]) then return 0 end
if redis.call('HGET', KEYS[2], ARGV[1]) ~= ARGV[2] then return 0 end
redis.call('ZADD', KEYS[1], ARGV[4], ARGV[1])
redis.call('EXPIRE', KEYS[1], math.max(tonumber(ARGV[5]), redis.call('TTL', KEYS[1])))
redis.call('EXPIRE', KEYS[2], math.max(tonumber(ARGV[5]), redis.call('TTL', KEYS[2])))
return 1
"""

_RELEASE_SCRIPT = """
-- LEASE_RELEASE
if ARGV[2] ~= '' and redis.call('HGET', KEYS[2], ARGV[1]) ~= ARGV[2] then return 0 end
redis.call('HDEL', KEYS[2], ARGV[1])
return redis.call('ZREM', KEYS[1], ARGV[1])
"""

_RESERVE_SCRIPT = """
-- DAILY_RESERVE
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
if cur + tonumber(ARGV[1]) > tonumber(ARGV[2]) then return 0 end
redis.call('INCRBY', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""

_SETTLE_SCRIPT = """
-- DAILY_SETTLE
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
local v = redis.call('INCRBY', KEYS[1], ARGV[1])
if v < 0 then redis.call('SET', KEYS[1], 0, 'KEEPTTL') return 0 end
return v
"""


_USAGE_SCRIPT = """
-- CURRENT_USAGE
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
return {redis.call('ZCARD', KEYS[1]), tonumber(redis.call('GET', KEYS[2]) or '0')}
"""


class ExecutionAlreadyAdmitted(Exception):
    """A duplicate delivery must not dispatch or terminate the active run."""


class AccountingUnavailable(Exception):
    """The accounting store could not answer; admission must fail closed."""


class SandboxAccounting(Protocol):
    async def acquire_lease(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        max_concurrency: int,
        ttl_seconds: int,
        owner_token: UUID | None = None,
    ) -> bool: ...

    async def renew_lease(
        self, tenant_id: UUID, run_id: UUID, *, owner_token: UUID, ttl_seconds: int
    ) -> None: ...

    async def release_lease(
        self, tenant_id: UUID, run_id: UUID, *, owner_token: UUID | None = None
    ) -> None: ...

    async def usage(self, tenant_id: UUID) -> tuple[int, int]: ...

    async def reserve_daily(
        self, tenant_id: UUID, *, seconds: int, daily_cap_s: int, day: datetime | None = None
    ) -> bool: ...

    async def settle_daily(
        self,
        tenant_id: UUID,
        *,
        reserved_seconds: int,
        actual_seconds: int,
        day: datetime | None = None,
    ) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _daily_key(tenant_id: UUID, now: datetime) -> str:
    return f"{_DAILY_PREFIX}:{tenant_id}:{now:%Y%m%d}"


def _seconds_until_utc_midnight(now: datetime) -> int:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, math.ceil((tomorrow - now).total_seconds())) + 3600


_ASYNC_POOLS: dict[tuple[str, asyncio.AbstractEventLoop], Any] = {}


async def aclose_sandbox_accounting_pools() -> None:
    """Retire this loop's clients before API/task shutdown, preserving other loops."""
    loop = asyncio.get_running_loop()
    for key in list(_ASYNC_POOLS):
        owner = key[1]
        if owner is loop:
            client = _ASYNC_POOLS.pop(key, None)
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    _log.warning("sandbox.accounting_pool_close_failed")
        elif owner.is_closed():
            _ASYNC_POOLS.pop(key, None)


class RedisSandboxAccounting:
    """Redis-Lua accounting; one pooled async client per (url, loop)."""

    def __init__(
        self,
        redis_url: str,
        *,
        client_factory: Callable[[], Any] | None = None,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._redis_url = redis_url
        self._client_factory = client_factory
        self._now = now
        self._pool: Any | None = None
        self._pool_loop: asyncio.AbstractEventLoop | None = None

    def _client(self) -> Any:
        loop = asyncio.get_running_loop()
        if self._client_factory is not None:
            if self._pool is None or self._pool_loop is not loop:
                self._pool = self._client_factory()
                self._pool_loop = loop
            return self._pool
        key = (self._redis_url, loop)
        client = _ASYNC_POOLS.get(key)
        if client is None:
            client = aioredis.from_url(  # type: ignore[no-untyped-call]
                self._redis_url,
                socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
                socket_timeout=_SOCKET_TIMEOUT_SECONDS,
            )
            _ASYNC_POOLS[key] = client
        return client

    async def _eval(self, script: str, key: str, *args: object) -> int:
        try:
            result = await self._client().eval(script, 1, key, *[str(a) for a in args])
            return int(result)
        except (redis.RedisError, TypeError, ValueError) as exc:
            raise AccountingUnavailable("sandbox accounting store unavailable") from exc

    async def _lease_eval(self, script: str, tenant_id: UUID, *args: object) -> int:
        key = f"{_LEASE_PREFIX}:{tenant_id}"
        try:
            result = await self._client().eval(
                script, 2, key, f"{key}:owners", *[str(a) for a in args]
            )
            return int(result)
        except (redis.RedisError, TypeError, ValueError) as exc:
            raise AccountingUnavailable("sandbox accounting store unavailable") from exc

    async def acquire_lease(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        max_concurrency: int,
        ttl_seconds: int,
        owner_token: UUID | None = None,
    ) -> bool:
        now = self._now().timestamp()
        admitted = await self._lease_eval(
            _ACQUIRE_SCRIPT,
            tenant_id,
            str(run_id),
            max_concurrency,
            now + ttl_seconds,
            now,
            ttl_seconds,
            owner_token or run_id,
        )
        if admitted == 2:
            raise ExecutionAlreadyAdmitted
        return admitted == 1

    async def renew_lease(
        self, tenant_id: UUID, run_id: UUID, *, owner_token: UUID, ttl_seconds: int
    ) -> None:
        now = self._now().timestamp()
        renewed = await self._lease_eval(
            _RENEW_SCRIPT, tenant_id, run_id, owner_token, now, now + ttl_seconds, ttl_seconds
        )
        if renewed != 1:
            raise AccountingUnavailable("sandbox lease ownership lost")

    async def usage(self, tenant_id: UUID) -> tuple[int, int]:
        now = self._now()
        try:
            values = await self._client().eval(
                _USAGE_SCRIPT,
                2,
                f"{_LEASE_PREFIX}:{tenant_id}",
                _daily_key(tenant_id, now),
                str(now.timestamp()),
            )
            return int(values[0]), int(values[1])
        except (redis.RedisError, TypeError, ValueError, IndexError) as exc:
            raise AccountingUnavailable("sandbox usage unavailable") from exc

    async def release_lease(
        self, tenant_id: UUID, run_id: UUID, *, owner_token: UUID | None = None
    ) -> None:
        try:
            await self._lease_eval(_RELEASE_SCRIPT, tenant_id, run_id, owner_token or "")
        except AccountingUnavailable:
            _log.warning("sandbox.lease_release_failed", tenant_id=str(tenant_id))

    async def reserve_daily(
        self, tenant_id: UUID, *, seconds: int, daily_cap_s: int, day: datetime | None = None
    ) -> bool:
        now = day or self._now()
        admitted = await self._eval(
            _RESERVE_SCRIPT,
            _daily_key(tenant_id, now),
            seconds,
            daily_cap_s,
            _seconds_until_utc_midnight(now),
        )
        return admitted == 1

    async def settle_daily(
        self,
        tenant_id: UUID,
        *,
        reserved_seconds: int,
        actual_seconds: int,
        day: datetime | None = None,
    ) -> None:
        try:
            await self._eval(
                _SETTLE_SCRIPT,
                _daily_key(tenant_id, day or self._now()),
                actual_seconds - reserved_seconds,
            )
        except AccountingUnavailable:
            _log.warning("sandbox.daily_settle_failed", tenant_id=str(tenant_id))


class InMemorySandboxAccounting:
    """Single-process accounting with identical semantics (tests/dev seam).

    NOT a production substitute: leases and counters live in this process,
    so multiple API/worker processes would each enforce their own cap.
    Production wiring always builds :class:`RedisSandboxAccounting`.
    """

    def __init__(self, *, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now
        self._lock = asyncio.Lock()
        self._leases: dict[UUID, dict[UUID, float]] = {}
        self._owners: dict[tuple[UUID, UUID], UUID] = {}
        self._daily: dict[str, int] = {}

    async def acquire_lease(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        max_concurrency: int,
        ttl_seconds: int,
        owner_token: UUID | None = None,
    ) -> bool:
        async with self._lock:
            now = self._now().timestamp()
            members = {m: e for m, e in self._leases.get(tenant_id, {}).items() if e > now}
            if run_id in members:
                raise ExecutionAlreadyAdmitted
            if len(members) >= max_concurrency:
                self._leases[tenant_id] = members
                return False
            members[run_id] = now + ttl_seconds
            self._leases[tenant_id] = members
            self._owners[tenant_id, run_id] = owner_token or run_id
            return True

    async def renew_lease(
        self, tenant_id: UUID, run_id: UUID, *, owner_token: UUID, ttl_seconds: int
    ) -> None:
        async with self._lock:
            now = self._now().timestamp()
            members = self._leases.get(tenant_id, {})
            if (
                members.get(run_id, 0) <= now
                or self._owners.get((tenant_id, run_id)) != owner_token
            ):
                raise AccountingUnavailable("sandbox lease ownership lost")
            members[run_id] = now + ttl_seconds

    async def usage(self, tenant_id: UUID) -> tuple[int, int]:
        async with self._lock:
            now = self._now()
            return (
                sum(
                    expiry > now.timestamp() for expiry in self._leases.get(tenant_id, {}).values()
                ),
                self._daily.get(_daily_key(tenant_id, now), 0),
            )

    async def release_lease(
        self, tenant_id: UUID, run_id: UUID, *, owner_token: UUID | None = None
    ) -> None:
        async with self._lock:
            if owner_token is not None and self._owners.get((tenant_id, run_id)) != owner_token:
                return
            self._leases.get(tenant_id, {}).pop(run_id, None)
            self._owners.pop((tenant_id, run_id), None)

    async def reserve_daily(
        self, tenant_id: UUID, *, seconds: int, daily_cap_s: int, day: datetime | None = None
    ) -> bool:
        async with self._lock:
            key = _daily_key(tenant_id, day or self._now())
            if self._daily.get(key, 0) + seconds > daily_cap_s:
                return False
            self._daily[key] = self._daily.get(key, 0) + seconds
            return True

    async def settle_daily(
        self,
        tenant_id: UUID,
        *,
        reserved_seconds: int,
        actual_seconds: int,
        day: datetime | None = None,
    ) -> None:
        async with self._lock:
            key = _daily_key(tenant_id, day or self._now())
            self._daily[key] = max(0, self._daily.get(key, 0) + actual_seconds - reserved_seconds)


__all__ = [
    "AccountingUnavailable",
    "ExecutionAlreadyAdmitted",
    "aclose_sandbox_accounting_pools",
    "InMemorySandboxAccounting",
    "RedisSandboxAccounting",
    "SandboxAccounting",
]
