"""Per-tenant fetch rate-limit tests (ADR-0009 §3, #20, risk:security).

Covers the load-bearing per-tenant fetch rate limit the review found missing:

* the Redis-backed fixed-window limiter admits the first ``max_per_window`` syncs
  per tenant and **throttles** the rest (the mandatory **limit-exhaustion
  negative**); the window is per-tenant (one tenant's spend does not throttle
  another); a Redis outage **fails open** (availability over strictness);
* the single enqueue point (:func:`app.tasks.enqueue_source_sync`) **defers** a
  throttled sync — it re-enqueues the Celery task with a ``countdown`` backoff
  rather than dropping it or surfacing an HTTP error (the /sources contract is
  frozen — no 429).

All offline: the Redis client is a tiny in-memory fake (no server), and the
broker publish is a recording stub (no broker).
"""

from __future__ import annotations

import asyncio
import importlib
import uuid

import pytest
import redis

from app.tasks.rate_limit import RateLimiter, RedisFixedWindowRateLimiter

# ``app.tasks.__init__`` re-exports the *task* object as ``sync_source``, which
# shadows the submodule of the same name on the package — so ``import
# app.tasks.sync_source as sync_mod`` would bind the task, not the module. Pull
# the real module out of the import system explicitly.
sync_mod = importlib.import_module("app.tasks.sync_source")


class _FakeRedis:
    """Minimal in-memory stand-in for ``redis.Redis`` (INCR/EXPIRE/close only).

    Backs the fixed-window limiter without a server. ``expirable`` records which
    keys had a TTL armed so a test can assert the window is set on the first hit.
    """

    store: dict[str, int] = {}
    expired: dict[str, int] = {}
    from_url_kwargs: dict[str, object] = {}

    def __init__(self) -> None:
        # Shared class-level dicts so every ``from_url`` returns the same view —
        # the production limiter opens a fresh client per call.
        pass

    @classmethod
    def reset(cls) -> None:
        cls.store = {}
        cls.expired = {}
        cls.from_url_kwargs = {}

    def incr(self, key: str) -> int:
        _FakeRedis.store[key] = _FakeRedis.store.get(key, 0) + 1
        return _FakeRedis.store[key]

    def expire(self, key: str, seconds: int) -> bool:
        _FakeRedis.expired[key] = seconds
        return True

    def close(self) -> None:  # noqa: D401 - parity with redis.Redis
        return None


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> type[_FakeRedis]:
    _FakeRedis.reset()

    def _from_url(cls: type, url: str, **kwargs: object) -> _FakeRedis:
        _FakeRedis.from_url_kwargs = dict(kwargs)
        return _FakeRedis()

    monkeypatch.setattr(redis.Redis, "from_url", classmethod(_from_url))
    return _FakeRedis


# --- the fixed-window limiter ----------------------------------------------


def test_limiter_admits_up_to_limit_then_throttles(fake_redis: type[_FakeRedis]) -> None:
    """The first ``max_per_window`` acquisitions pass; the next is throttled.

    The mandatory limit-exhaustion **negative**: once the window budget is spent
    the limiter returns ``False`` so the caller must defer.
    """
    limiter = RedisFixedWindowRateLimiter(
        "redis://localhost:6379/0", max_per_window=3, window_seconds=60
    )
    tenant = uuid.uuid4()

    assert [limiter.try_acquire(tenant) for _ in range(3)] == [True, True, True]
    # 4th in the window is over budget → throttled.
    assert limiter.try_acquire(tenant) is False
    assert limiter.try_acquire(tenant) is False  # stays throttled


def test_limiter_arms_window_ttl_on_first_hit(fake_redis: type[_FakeRedis]) -> None:
    """The window TTL is set exactly once (on the first increment)."""
    limiter = RedisFixedWindowRateLimiter(
        "redis://localhost:6379/0", max_per_window=5, window_seconds=42
    )
    tenant = uuid.uuid4()
    limiter.try_acquire(tenant)
    limiter.try_acquire(tenant)
    key = f"lumen:ratelimit:source_sync:{tenant}"
    assert fake_redis.expired == {key: 42}


def test_limiter_client_carries_socket_timeouts(fake_redis: type[_FakeRedis]) -> None:
    """#271: the sync client is bounded — a Redis blip must fail fast, not hang
    whatever thread runs the enqueue."""
    limiter = RedisFixedWindowRateLimiter(
        "redis://localhost:6379/0", max_per_window=1, window_seconds=60
    )
    limiter.try_acquire(uuid.uuid4())
    assert fake_redis.from_url_kwargs.get("socket_connect_timeout") == 2.0
    assert fake_redis.from_url_kwargs.get("socket_timeout") == 2.0


def test_limiter_is_per_tenant(fake_redis: type[_FakeRedis]) -> None:
    """One tenant's spend does not throttle another (the window is per-tenant)."""
    limiter = RedisFixedWindowRateLimiter(
        "redis://localhost:6379/0", max_per_window=1, window_seconds=60
    )
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    assert limiter.try_acquire(tenant_a) is True
    assert limiter.try_acquire(tenant_a) is False  # A exhausted
    assert limiter.try_acquire(tenant_b) is True  # B unaffected


def test_limiter_fails_open_on_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Redis outage admits the sync (availability; SSRF guard stays authoritative)."""

    def _boom(cls: type, url: str, **kwargs: object) -> object:
        raise redis.ConnectionError("redis down")

    monkeypatch.setattr(redis.Redis, "from_url", classmethod(_boom))
    limiter = RedisFixedWindowRateLimiter(
        "redis://localhost:6379/0", max_per_window=1, window_seconds=60
    )
    assert limiter.try_acquire(uuid.uuid4()) is True


# --- the enqueue boundary defers a throttled sync ---------------------------


class _RecordingBrokerConnection:
    """Context-manager stand-in for ``celery_app.connection_for_write()``."""

    def __enter__(self) -> _RecordingBrokerConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def ensure_connection(self, **_kwargs: object) -> None:
        return None


@pytest.fixture
def captured_publish(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Capture ``sync_source.apply_async`` calls without a broker."""
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        sync_mod.celery_app,
        "connection_for_write",
        lambda: _RecordingBrokerConnection(),
    )

    def _apply_async(*, args: tuple, connection: object, retry: bool, countdown: int) -> None:
        calls.append({"args": args, "countdown": countdown})

    monkeypatch.setattr(sync_mod.sync_source, "apply_async", _apply_async)
    return calls


class _AlwaysLimiter:
    """A deterministic limiter for the enqueue-deferral tests."""

    def __init__(self, admit: bool) -> None:
        self._admit = admit

    def try_acquire(self, tenant_id: uuid.UUID) -> bool:
        return self._admit


def test_enqueue_admitted_publishes_immediately(
    captured_publish: list[dict[str, object]],
) -> None:
    """Within budget: the sync is published with no backoff (``countdown == 0``)."""
    tenant, source = uuid.uuid4(), uuid.uuid4()
    limiter: RateLimiter = _AlwaysLimiter(admit=True)
    sync_mod.enqueue_source_sync(tenant, source, rate_limiter=limiter)

    assert len(captured_publish) == 1
    assert captured_publish[0]["args"] == (str(tenant), str(source))
    assert captured_publish[0]["countdown"] == 0


def test_enqueue_throttled_defers_with_backoff(
    captured_publish: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Limit-exhaustion **negative**: a throttled sync is deferred, not dropped.

    The task is still enqueued (never lost) but with a positive ``countdown``
    backoff, and **no** HTTP error is raised — the frozen /sources contract has
    no 429, so the throttle is invisible to the wire.
    """
    # Pin the configured backoff so the assertion is exact.
    from app.core.config import Settings, get_settings

    def _settings() -> Settings:
        return Settings(  # type: ignore[call-arg]
            DATABASE_URL="sqlite+aiosqlite://",
            REDIS_URL="redis://localhost:6379/0",
            CELERY_BROKER_URL="redis://localhost:6379/1",
            CELERY_RESULT_BACKEND="redis://localhost:6379/2",
            S3_ENDPOINT_URL="http://localhost:9000",
            S3_ACCESS_KEY="k",
            S3_SECRET_KEY="s",
            S3_BUCKET="b",
            OPENROUTER_API_KEY="",
            SOURCE_SYNC_RATE_BACKOFF_SECONDS="30",
        )

    monkeypatch.setattr(sync_mod, "get_settings", _settings)
    get_settings.cache_clear()

    tenant, source = uuid.uuid4(), uuid.uuid4()
    limiter: RateLimiter = _AlwaysLimiter(admit=False)
    # Must not raise — the throttle defers, it does not error.
    sync_mod.enqueue_source_sync(tenant, source, rate_limiter=limiter)

    assert len(captured_publish) == 1  # still enqueued (not dropped)
    assert captured_publish[0]["args"] == (str(tenant), str(source))
    assert captured_publish[0]["countdown"] == 30  # deferred with backoff


# --- The async, pooled entry point (#527) -----------------------------------
#
# The limiter began on the Celery sync-enqueue boundary, where blocking is free.
# The request-path callers that arrived later — the ``web_search`` tool and MCP
# egress — admission-check inside a live chat turn, where the sync form parks the
# serving loop on a fresh TCP connect plus INCR for every tool call. These pin
# that the async form is semantically identical, pools its connection, and stays
# off the loop.


class _FakeAsyncRedis:
    """In-memory async stand-in; counts how many clients were constructed."""

    built = 0
    store: dict[str, int] = {}
    expired: dict[str, int] = {}

    def __init__(self, *, fail: bool = False) -> None:
        _FakeAsyncRedis.built += 1
        self._fail = fail
        self.closed = False

    @classmethod
    def reset(cls) -> None:
        cls.built = 0
        cls.store = {}
        cls.expired = {}

    async def incr(self, key: str) -> int:
        if self._fail:
            raise redis.RedisError("counter store unreachable")
        _FakeAsyncRedis.store[key] = _FakeAsyncRedis.store.get(key, 0) + 1
        return _FakeAsyncRedis.store[key]

    async def expire(self, key: str, seconds: int) -> bool:
        _FakeAsyncRedis.expired[key] = seconds
        return True

    async def aclose(self) -> None:
        self.closed = True


def _async_limiter(*, max_per_window: int = 3, fail: bool = False) -> RedisFixedWindowRateLimiter:
    return RedisFixedWindowRateLimiter(
        "redis://localhost:6379/0",
        max_per_window=max_per_window,
        window_seconds=60,
        async_client_factory=lambda: _FakeAsyncRedis(fail=fail),
    )


async def test_async_limiter_matches_the_sync_window_semantics() -> None:
    """Same budget, same window arming, same per-tenant isolation."""
    _FakeAsyncRedis.reset()
    limiter = _async_limiter(max_per_window=3)
    tenant, other = uuid.uuid4(), uuid.uuid4()

    assert [await limiter.try_acquire_async(tenant) for _ in range(3)] == [True, True, True]
    assert await limiter.try_acquire_async(tenant) is False  # window exhausted
    assert await limiter.try_acquire_async(tenant) is False  # stays throttled
    # A different tenant's budget is untouched.
    assert await limiter.try_acquire_async(other) is True
    # The TTL is armed once, on the first hit of each tenant's window.
    assert _FakeAsyncRedis.expired == {
        f"lumen:ratelimit:source_sync:{tenant}": 60,
        f"lumen:ratelimit:source_sync:{other}": 60,
    }


async def test_async_limiter_reuses_one_pooled_client_across_calls() -> None:
    """The point of the async path: one connection, not one per admission check.

    The sync form opens a client per call (see ``_FakeRedis``' docstring); on the
    chat path that is a TCP connect per tool invocation.
    """
    _FakeAsyncRedis.reset()
    limiter = _async_limiter()
    tenant = uuid.uuid4()

    for _ in range(5):
        await limiter.try_acquire_async(tenant)

    assert _FakeAsyncRedis.built == 1


async def test_async_limiter_fails_open_and_drops_the_dead_client() -> None:
    """A counter-store outage admits (as the sync path does) and does not pin a bad client."""
    _FakeAsyncRedis.reset()
    limiter = _async_limiter(fail=True)

    assert await limiter.try_acquire_async(uuid.uuid4()) is True
    assert await limiter.try_acquire_async(uuid.uuid4()) is True
    # Each failure retires the pooled client rather than reusing a broken one.
    assert _FakeAsyncRedis.built == 2


def test_async_limiter_rebuilds_its_client_for_a_new_event_loop() -> None:
    """A client built on a dead loop is never handed out (the #140 failure mode).

    Celery runs each task on a fresh loop, so a limiter that cached one client
    forever would hand a warm worker connections belonging to a closed loop.

    Deliberately a *sync* test: it drives two independent loops itself, which it
    could not do from inside one (``asyncio_mode = "auto"``).
    """
    _FakeAsyncRedis.reset()
    limiter = _async_limiter()

    async def _use() -> None:
        await limiter.try_acquire_async(uuid.uuid4())

    asyncio.run(_use())
    asyncio.run(_use())  # a second, independent loop

    assert _FakeAsyncRedis.built == 2


async def test_async_limiter_aclose_releases_the_pool() -> None:
    """Shutdown releases the pooled client, and a later call rebuilds."""
    _FakeAsyncRedis.reset()
    limiter = _async_limiter()
    await limiter.try_acquire_async(uuid.uuid4())
    pooled = limiter._pool  # noqa: SLF001 - asserting the lifecycle it owns

    await limiter.aclose()

    assert pooled.closed is True
    assert limiter._pool is None  # noqa: SLF001
    await limiter.try_acquire_async(uuid.uuid4())
    assert _FakeAsyncRedis.built == 2


async def test_separate_limiter_instances_share_one_process_wide_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The property that makes the pooling worth anything in production.

    ``build_web_search_service`` / ``build_mcp_servers_service`` construct a
    **fresh** limiter per request, so a pool owned by the instance would still
    open a connection per search. This drives the real (non-injected) path with
    ``aioredis.from_url`` stubbed, and asserts a second limiter for the same URL
    reuses the first one's client.
    """
    import app.tasks.rate_limit as rl

    built: list[_FakeAsyncRedis] = []

    def _from_url(_url: str, **_kwargs: object) -> _FakeAsyncRedis:
        client = _FakeAsyncRedis()
        built.append(client)
        return client

    _FakeAsyncRedis.reset()
    monkeypatch.setattr(rl.aioredis, "from_url", _from_url)
    monkeypatch.setattr(rl, "_ASYNC_POOLS", {})

    def _fresh() -> RedisFixedWindowRateLimiter:
        return RedisFixedWindowRateLimiter(
            "redis://localhost:6379/0", max_per_window=100, window_seconds=60
        )

    await _fresh().try_acquire_async(uuid.uuid4())
    await _fresh().try_acquire_async(uuid.uuid4())
    await _fresh().try_acquire_async(uuid.uuid4())

    assert len(built) == 1

    # A different Redis URL is a different pool entry, not a silently shared one.
    other = RedisFixedWindowRateLimiter(
        "redis://elsewhere:6379/1", max_per_window=100, window_seconds=60
    )
    await other.try_acquire_async(uuid.uuid4())
    assert len(built) == 2

    await rl.aclose_async_rate_limit_pools()
    assert all(c.closed for c in built)
