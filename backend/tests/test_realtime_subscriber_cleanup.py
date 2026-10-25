"""Subscriber owns both Redis sockets even during AnyIO cancellation (#803)."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import pytest

from app.realtime import envelopes
from tests.test_realtime_backplane_pool import _fake_backplane


@pytest.mark.parametrize("phase", ["unsubscribe", "pubsub_close", "client_close"])
async def test_level_cancellation_at_each_cleanup_checkpoint_closes_both_handles(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    backplane, server = _fake_backplane()
    await backplane.publish("s", envelopes.done("s", 0))
    checkpoint = anyio.Event()
    cancelled = False
    # Start the subscriber before patching its real handles, then let it close.
    subscription = backplane.subscribe("s")
    assert (await anext(subscription))["type"] == "done"
    client = server.clients[1]
    pubsub = client.pubsubs[0]
    owner, name = {
        "unsubscribe": (pubsub, "unsubscribe"),
        "pubsub_close": (pubsub, "aclose"),
        "client_close": (client, "aclose"),
    }[phase]
    original: Callable[..., Awaitable[None]] = getattr(owner, name)

    async def with_checkpoint(*args: Any) -> None:
        checkpoint.set()
        await anyio.lowlevel.checkpoint()
        await original(*args)

    monkeypatch.setattr(owner, name, with_checkpoint)

    async def consume() -> None:
        nonlocal cancelled
        try:
            await subscription.aclose()
            await anyio.lowlevel.checkpoint()
        except anyio.get_cancelled_exc_class():
            cancelled = True
            raise

    async with anyio.create_task_group() as group:
        group.start_soon(consume)
        await checkpoint.wait()
        group.cancel_scope.cancel()
    assert cancelled
    assert pubsub.closed
    assert client.closed
    assert not server.clients[0].closed
    await backplane.aclose()


async def test_unsubscribe_failure_still_closes_both_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backplane, server = _fake_backplane()
    await backplane.publish("s", envelopes.done("s", 0))
    subscription = backplane.subscribe("s")
    await anext(subscription)
    client = server.clients[1]
    pubsub = client.pubsubs[0]

    async def fail(channel: str) -> None:
        raise RuntimeError("synthetic unsubscribe failure")

    monkeypatch.setattr(pubsub, "unsubscribe", fail)
    with pytest.raises(RuntimeError, match="synthetic unsubscribe failure"):
        await anext(subscription)
    assert pubsub.closed
    assert client.closed
    await backplane.aclose()


async def test_consumer_error_is_preserved_when_cleanup_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backplane, server = _fake_backplane()
    await backplane.publish("s", envelopes.done("s", 0))
    subscription = backplane.subscribe("s")
    await anext(subscription)
    client = server.clients[1]

    async def fail(channel: str) -> None:
        raise RuntimeError("synthetic cleanup failure")

    monkeypatch.setattr(client.pubsubs[0], "unsubscribe", fail)
    original = ValueError("synthetic consumer failure")
    with pytest.raises(ValueError) as raised:
        try:
            raise original
        finally:
            await subscription.aclose()
    assert raised.value is original
    assert client.closed
    assert client.pubsubs[0].closed
    await backplane.aclose()


async def test_hung_cleanup_steps_share_total_existing_grace_and_attempt_all_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backplane, server = _fake_backplane()
    monkeypatch.setattr(backplane, "_cleanup_grace_seconds", 0.09)
    await backplane.publish("s", envelopes.done("s", 0))
    subscription = backplane.subscribe("s")
    await anext(subscription)
    client = server.clients[1]
    pubsub = client.pubsubs[0]
    attempted: list[str] = []

    async def unsubscribe(channel: str) -> None:
        attempted.append("unsubscribe")
        await anyio.sleep_forever()

    async def close_pubsub() -> None:
        attempted.append("pubsub_close")
        pubsub.closed = True
        await anyio.sleep_forever()

    async def close_client() -> None:
        attempted.append("client_close")
        client.closed = True
        await anyio.sleep_forever()

    monkeypatch.setattr(pubsub, "unsubscribe", unsubscribe)
    monkeypatch.setattr(pubsub, "aclose", close_pubsub)
    monkeypatch.setattr(client, "aclose", close_client)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await anext(subscription)
    assert 0.07 <= time.monotonic() - started < 0.5
    assert attempted == ["unsubscribe", "pubsub_close", "client_close"]
    assert pubsub.closed and client.closed
    await backplane.aclose()


def test_dependency_passes_existing_configured_shutdown_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import deps
    from app.core.config import Settings

    settings = Settings(CHAT_SHUTDOWN_GRACE_SECONDS=0.9)
    captured: dict[str, Any] = {}

    def factory(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(deps, "RedisBackplane", factory)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    deps.get_backplane.cache_clear()
    try:
        deps.get_backplane()
        assert captured["cleanup_grace_seconds"] == 0.9
    finally:
        deps.get_backplane.cache_clear()
