"""Opt-in isolated Redis proof for atomic admission (#547); never flushes Redis."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import redis
import redis.asyncio as aioredis

from app.core.config import get_settings
from app.tasks.rate_limit import RedisFixedWindowRateLimiter, aclose_async_rate_limit_pools

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1 for real Redis"),
]


async def test_atomic_limiter_real_concurrency_ttl_cancellation_and_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = get_settings().redis_url
    prefix = f"lumen:qa547:{uuid4()}"
    client = aioredis.from_url(url)
    commands: list[str] = []
    sync_execute = redis.Redis.execute_command
    async_execute = aioredis.Redis.execute_command

    def record_sync(self: redis.Redis, *args: object, **kwargs: object) -> object:
        if args[0] in {"INCR", "INCRBY", "EXPIRE", "EVAL"}:
            commands.append(str(args[0]))
        return sync_execute(self, *args, **kwargs)

    async def record_async(self: aioredis.Redis, *args: object, **kwargs: object) -> object:
        if args[0] in {"INCR", "INCRBY", "EXPIRE", "EVAL"}:
            commands.append(str(args[0]))
        return await async_execute(self, *args, **kwargs)

    monkeypatch.setattr(redis.Redis, "execute_command", record_sync)
    monkeypatch.setattr(aioredis.Redis, "execute_command", record_async)

    def limiter(suffix: str, *, cap: int = 7, window: int = 20) -> RedisFixedWindowRateLimiter:
        return RedisFixedWindowRateLimiter(
            url, max_per_window=cap, window_seconds=window, key_prefix=f"{prefix}:{suffix}"
        )

    cancelled_client = None
    task = None
    try:
        # Prior first-hit/later-hit command sequence, measured against this same server.
        baseline = f"{prefix}:baseline"
        assert await client.incr(baseline) == 1
        await client.expire(baseline, 20)
        await client.incr(baseline)
        assert commands == ["INCRBY", "EXPIRE", "INCRBY"]
        commands.clear()
        tenant = uuid4()
        current = limiter("commands")
        assert current.try_acquire(tenant)
        assert await current.try_acquire_async(tenant)
        assert commands == ["EVAL", "EVAL"]

        # Concurrent sync and async callers spend exactly one shared tenant budget.
        concurrent = limiter("concurrent")
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=4) as pool:
            admitted = await asyncio.gather(
                *(loop.run_in_executor(pool, concurrent.try_acquire, tenant) for _ in range(20)),
                *(concurrent.try_acquire_async(tenant) for _ in range(20)),
            )
        assert sum(admitted) == 7
        key = f"{prefix}:concurrent:{tenant}"
        assert int(await client.get(key)) == 40
        assert 0 < await client.pttl(key) <= 20_000
        assert await concurrent.try_acquire_async(uuid4())  # independent tenant
        assert await limiter("other-prefix").try_acquire_async(tenant)

        # A first-hit window expires; subsequent denied hits never extend it.
        short = limiter("short", cap=1, window=2)
        assert short.try_acquire(tenant)
        short_key = f"{prefix}:short:{tenant}"
        before = await client.pttl(short_key)
        await asyncio.sleep(0.1)
        assert not await short.try_acquire_async(tenant)
        after = await client.pttl(short_key)
        assert 0 < after < before
        await asyncio.sleep(2.1)
        assert not await client.exists(short_key)
        assert await short.try_acquire_async(tenant)

        # Delay delivery of the real server result, then cancel the caller. The
        # server has completed the script; no second client operation is needed.
        written = asyncio.Event()
        cancelled_client = aioredis.from_url(url)

        class DelayedReply:
            async def eval(self, *args: object) -> object:
                result = await cancelled_client.eval(*args)
                written.set()
                await asyncio.Future()
                return result

        cancelled = RedisFixedWindowRateLimiter(
            url,
            max_per_window=1,
            window_seconds=2,
            key_prefix=f"{prefix}:cancel",
            async_client_factory=DelayedReply,
        )
        task = asyncio.create_task(cancelled.try_acquire_async(tenant))
        await asyncio.wait_for(written.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cancelled_key = f"{prefix}:cancel:{tenant}"
        assert 0 < await client.pttl(cancelled_key) <= 2000
        await asyncio.sleep(2.1)
        assert not await client.exists(cancelled_key)

        # Abrupt sync-caller exit after Redis's first counter command, before the
        # limiter returns. The former split implementation would leave no TTL.
        child_code = """
import os, sys, uuid, redis
from app.tasks.rate_limit import RedisFixedWindowRateLimiter
execute = redis.Redis.execute_command
def interrupted(self, *args, **kwargs):
    result = execute(self, *args, **kwargs)
    if args[0] in {'EVAL', 'INCRBY', 'INCR'}:
        os._exit(23)
    return result
redis.Redis.execute_command = interrupted
RedisFixedWindowRateLimiter(sys.argv[1], max_per_window=1, window_seconds=2,
    key_prefix=sys.argv[2]).try_acquire(uuid.UUID(sys.argv[3]))
"""
        child = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", child_code, url, f"{prefix}:process", str(tenant)],
            timeout=30,
            check=False,
            capture_output=True,
        )
        assert child.returncode == 23, child.stderr.decode()
        process_key = f"{prefix}:process:{tenant}"
        assert 0 < await client.pttl(process_key) <= 2000
        await asyncio.sleep(2.1)
        assert not await client.exists(process_key)
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if cancelled_client is not None:
            await cancelled_client.aclose()
        await aclose_async_rate_limit_pools()
        keys = [key async for key in client.scan_iter(f"{prefix}:*")]
        if keys:
            await client.delete(*keys)
        assert [key async for key in client.scan_iter(f"{prefix}:*")] == []
        await client.aclose()
