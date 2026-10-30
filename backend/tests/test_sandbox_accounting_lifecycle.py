"""Loop-owned Redis accounting clients retire with their API/task loop (#589)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from uuid import uuid4

import pytest

from app.sandbox import accounting
from app.tasks import runner as task_runner


class _Client:
    def __init__(self) -> None:
        self.owner: asyncio.AbstractEventLoop | None = None
        self.closed_on: asyncio.AbstractEventLoop | None = None

    async def eval(self, *args: object) -> int:
        self.owner = asyncio.get_running_loop()
        return 1

    async def aclose(self) -> None:
        self.closed_on = asyncio.get_running_loop()


def test_task_failure_retires_its_accounting_client(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client()
    url = f"redis://accounting-{uuid4()}"
    monkeypatch.setattr(accounting.aioredis, "from_url", lambda *args, **kwargs: client)

    async def noop() -> None:
        return None

    monkeypatch.setattr(task_runner, "dispose_engine", noop)
    monkeypatch.setattr(task_runner, "aclose_embedding_clients", noop)

    async def failing_task() -> None:
        store = accounting.RedisSandboxAccounting(url)
        assert await store.acquire_lease(uuid4(), uuid4(), max_concurrency=1, ttl_seconds=60)
        raise RuntimeError("execution failed")

    try:
        with pytest.raises(RuntimeError, match="execution failed"):
            task_runner.run_task(failing_task())
        assert client.closed_on is client.owner
        assert client.owner is not None and client.owner.is_closed()
    finally:
        for key in list(accounting._ASYNC_POOLS):
            if accounting._ASYNC_POOLS[key] is client:
                accounting._ASYNC_POOLS.pop(key)


async def test_accounting_cleanup_keeps_a_live_foreign_loop() -> None:
    loop = asyncio.get_running_loop()
    foreign = asyncio.new_event_loop()
    retired = asyncio.new_event_loop()
    retired.close()
    own_client, other_client, retired_client = _Client(), _Client(), _Client()
    prefix = str(uuid4())
    keys = [(prefix, loop), (prefix, foreign), (prefix, retired)]
    accounting._ASYNC_POOLS.update(
        zip(keys, [own_client, other_client, retired_client], strict=True)
    )
    try:
        await accounting.aclose_sandbox_accounting_pools()
        assert own_client.closed_on is loop
        assert other_client.closed_on is None
        assert keys[0] not in accounting._ASYNC_POOLS
        assert keys[1] in accounting._ASYNC_POOLS
        assert keys[2] not in accounting._ASYNC_POOLS
    finally:
        for key in keys:
            accounting._ASYNC_POOLS.pop(key, None)
        foreign.close()


def test_policy_can_be_imported_before_sandbox_services_in_a_cold_process() -> None:
    code = (
        "from app.services.sandbox_policy_service import SandboxPolicyReader\n"
        "from app.sandbox import SandboxService\n"
        "assert SandboxPolicyReader and SandboxService\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
