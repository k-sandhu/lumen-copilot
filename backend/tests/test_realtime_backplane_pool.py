"""Pooled-Redis-client backplane tests — one client per process/loop (#487).

Pre-#487 :class:`~app.realtime.backplane.RedisBackplane` opened a **brand-new**
Redis client per envelope and tore it down again, so a streamed answer paid a
connect/close cycle per provider token chunk. These tests pin the pooled
replacement:

* one client construction serves an arbitrary number of ``publish`` calls **and**
  ``bind_owner`` / ``get_owner`` (AC-1 of #487; also AC-3 of #492, which wants the
  ``202``'s owner binding off the per-call-connection path);
* the pooled client is released by :func:`~app.api.deps.aclose_backplane` and by
  the app lifespan, and a later publish transparently re-creates it (AC-5);
* a client built on a now-dead event loop is **never** reused — the #140
  engine-per-loop failure mode, and exactly what keeps the pool Celery-safe
  (AC-5);
* replay/live equivalence (#153) is unchanged by the pooling, asserted against
  the REAL ``RedisBackplane`` — the offline suite otherwise only exercises
  ``InMemoryBackplane``, whose in-process buffer *masks* the raw-pub/sub bug.

**No socket is opened.** The backplane takes an injectable ``client_factory``
(the seam that lets failure/lifecycle tests run without a live Redis); these
tests inject a fake whose semantics mirror exactly the commands the backplane
issues — ``RPUSH``/``LTRIM``/``EXPIRE``/``PUBLISH`` in one pipeline, ``LRANGE``,
``SET``/``GET``, and a pub/sub connection that emits the subscribe confirmation
first. The live-Redis regression stays in ``test_realtime_backplane_live.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from app.api import deps
from app.core.config import get_settings
from app.main import lifespan
from app.realtime import envelopes
from app.realtime.backplane import RedisBackplane, StreamOwner, is_terminal

# --- A fake Redis with just the commands the backplane issues ---------------


class _FakeServer:
    """Shared state behind every :class:`_FakeClient` a factory hands out."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.strings: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        self.channels: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        self.clients: list[_FakeClient] = []

    @property
    def created(self) -> int:
        return len(self.clients)

    def deliver(self, channel: str, payload: str) -> None:
        for queue in list(self.channels.get(channel, ())):
            queue.put_nowait({"type": "message", "channel": channel, "data": payload})


class _FakePipeline:
    """Buffers the pipeline's commands and applies them on ``execute``."""

    def __init__(self, server: _FakeServer) -> None:
        self._server = server
        self._ops: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def rpush(self, key: str, value: str) -> None:
        self._ops.append(("rpush", (key, value)))

    def ltrim(self, key: str, start: int, end: int) -> None:
        self._ops.append(("ltrim", (key, start, end)))

    def expire(self, key: str, ttl: int) -> None:
        self._ops.append(("expire", (key, ttl)))

    def publish(self, channel: str, payload: str) -> None:
        self._ops.append(("publish", (channel, payload)))

    async def execute(self) -> list[Any]:
        for name, args in self._ops:
            if name == "rpush":
                key, value = args
                self._server.lists.setdefault(key, []).append(value)
            elif name == "ltrim":
                key, start, end = args
                assert end == -1 and start < 0, "backplane only tail-trims"
                self._server.lists[key] = self._server.lists.get(key, [])[start:]
            elif name == "expire":
                key, ttl = args
                self._server.expires[key] = ttl
            elif name == "publish":
                channel, payload = args
                self._server.deliver(channel, payload)
        self._ops.clear()
        return []


class _FakePubSub:
    """One pub/sub connection: a queue fed by the server, confirmation first."""

    def __init__(self, server: _FakeServer) -> None:
        self._server = server
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self._server.channels.setdefault(channel, []).append(self._queue)
        # Redis acknowledges a SUBSCRIBE as the first message on the connection.
        self._queue.put_nowait({"type": "subscribe", "channel": channel, "data": 1})

    async def unsubscribe(self, channel: str) -> None:
        queues = self._server.channels.get(channel)
        if queues is not None and self._queue in queues:
            queues.remove(self._queue)

    async def get_message(
        self,
        timeout: float | None = None,  # noqa: ASYNC109 — mirrors redis-py's signature
    ) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None

    async def listen(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            yield await self._queue.get()

    async def aclose(self) -> None:
        self.closed = True


class _FakeClient:
    """A fake ``redis.asyncio`` client — only what the backplane calls."""

    def __init__(self, server: _FakeServer) -> None:
        self._server = server
        self.closed = False
        self.pubsubs: list[_FakePubSub] = []

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self._server)

    def pubsub(self) -> _FakePubSub:
        pubsub = _FakePubSub(self._server)
        self.pubsubs.append(pubsub)
        return pubsub

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        assert (start, end) == (0, -1), "backplane reads the whole replay list"
        return list(self._server.lists.get(key, []))

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._server.strings[key] = value
        if ex is not None:
            self._server.expires[key] = ex

    async def get(self, key: str) -> str | None:
        return self._server.strings.get(key)

    async def aclose(self) -> None:
        self.closed = True


def _fake_backplane() -> tuple[RedisBackplane, _FakeServer]:
    server = _FakeServer()

    def factory() -> Any:
        client = _FakeClient(server)
        server.clients.append(client)
        return client

    return RedisBackplane("redis://unused/0", client_factory=factory), server


# --- AC-1: client construction is O(1), not O(envelopes) --------------------


async def test_publishing_many_envelopes_constructs_one_client() -> None:
    """AC-1 (#487): a streamed answer's envelopes share ONE pooled client.

    Pre-fix this asserted 52 constructions (one per envelope) — a TCP connect
    and teardown serialised into streaming throughput.
    """
    backplane, server = _fake_backplane()
    stream_id = uuid.uuid4().hex

    await backplane.publish(stream_id, envelopes.start(stream_id, 0))
    for seq in range(1, 51):
        await backplane.publish(stream_id, envelopes.delta(stream_id, seq, {"text": "x"}))
    await backplane.publish(stream_id, envelopes.done(stream_id, 51))

    assert server.created == 1
    assert not server.clients[0].closed  # pooled: NOT torn down per envelope
    await backplane.aclose()


async def test_owner_binding_and_lookup_share_the_pooled_client() -> None:
    """AC-1 / #492 AC-3: ``bind_owner``/``get_owner`` reuse the same client."""
    backplane, server = _fake_backplane()
    stream_id = uuid.uuid4().hex
    owner = StreamOwner(owner_id=uuid.uuid4(), tenant_id=uuid.uuid4())

    await backplane.bind_owner(stream_id, owner)
    await backplane.publish(stream_id, envelopes.start(stream_id, 0))
    read_back = await backplane.get_owner(stream_id)

    assert read_back == owner
    assert server.created == 1
    await backplane.aclose()


async def test_bind_owner_opens_no_new_connection_per_call() -> None:
    """#492 AC-3: the ``202``'s ``bind_owner`` reuses the pooled client — repeated
    binds on the serving loop (many concurrent sends) construct exactly ONE
    client, never a connect/teardown per send. Pre-#487 each ``bind_owner`` built
    and closed its own Redis client, sitting on the synchronous send path inside
    the still-open Postgres transaction."""
    backplane, server = _fake_backplane()

    for _ in range(20):
        owner = StreamOwner(owner_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        await backplane.bind_owner(uuid.uuid4().hex, owner)

    assert server.created == 1  # O(1), not O(sends)
    assert not server.clients[0].closed  # pooled: not torn down per call
    await backplane.aclose()


async def test_unknown_stream_owner_is_none() -> None:
    """Negative: an unbound/expired stream id reads back as ``None`` (deny)."""
    backplane, server = _fake_backplane()

    assert await backplane.get_owner(uuid.uuid4().hex) is None
    assert server.created == 1
    await backplane.aclose()


# --- AC-3 (negative): replay/live equivalence survives the pooling ----------


async def _drain(backplane: RedisBackplane, stream_id: str) -> list[dict[str, Any]]:
    return [envelope async for envelope in backplane.subscribe(stream_id)]


async def test_late_subscriber_replays_the_whole_stream_from_the_pooled_publisher() -> None:
    """AC-3 (#153): the producer finishes first; the subscriber still gets it all."""
    backplane, _server = _fake_backplane()
    stream_id = uuid.uuid4().hex

    await backplane.publish(stream_id, envelopes.start(stream_id, 0))
    await backplane.publish(stream_id, envelopes.delta(stream_id, 1, {"text": "hi"}))
    await backplane.publish(stream_id, envelopes.done(stream_id, 2))

    received = await asyncio.wait_for(_drain(backplane, stream_id), timeout=2.0)

    assert [e["type"] for e in received] == ["start", "delta", "done"]
    assert [e["seq"] for e in received] == [0, 1, 2]
    assert sum(1 for e in received if is_terminal(e)) == 1
    await backplane.aclose()


async def test_subscriber_connected_first_receives_the_live_channel() -> None:
    """AC-3: the live leg is unchanged — and each envelope is relayed once."""
    backplane, _server = _fake_backplane()
    stream_id = uuid.uuid4().hex

    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)  # let SUBSCRIBE + the confirmation land

    await backplane.publish(stream_id, envelopes.start(stream_id, 0))
    await backplane.publish(stream_id, envelopes.delta(stream_id, 1, {"text": "hi"}))
    await backplane.publish(stream_id, envelopes.done(stream_id, 2))

    received = await asyncio.wait_for(consumer, timeout=2.0)

    assert [e["type"] for e in received] == ["start", "delta", "done"]
    assert [e["seq"] for e in received] == [0, 1, 2]
    await backplane.aclose()


def _done_pending(stream_id: str, seq: int) -> dict[str, Any]:
    return envelopes.done(stream_id, seq, data={"messageId": "m", "pendingSuggestions": True})


def _suggestions(stream_id: str, seq: int) -> dict[str, Any]:
    return envelopes.event(
        stream_id, seq, name="suggestions", data={"messageId": "m", "suggestions": ["Next?"]}
    )


async def test_pending_terminal_relays_post_terminal_suggestions_from_replay() -> None:
    """#489: the producer finished (start, done(pendingSuggestions), suggestions all
    buffered). The REAL RedisBackplane subscribe must fall THROUGH the replayed
    terminal (not return at it) and relay the trailing suggestions event."""
    backplane, _server = _fake_backplane()
    stream_id = uuid.uuid4().hex

    await backplane.publish(stream_id, envelopes.start(stream_id, 0))
    await backplane.publish(stream_id, _done_pending(stream_id, 1))
    await backplane.publish(stream_id, _suggestions(stream_id, 2))

    received = await asyncio.wait_for(_drain(backplane, stream_id), timeout=2.0)
    assert [e["type"] for e in received] == ["start", "done", "event"]
    assert received[-1]["name"] == "suggestions"
    assert sum(1 for e in received if is_terminal(e)) == 1
    await backplane.aclose()


async def test_pending_terminal_relays_live_post_terminal_suggestions() -> None:
    """#489: a subscriber connected BEFORE publishing gets ``done`` then the live
    post-terminal suggestions event over the live channel (grace window)."""
    backplane, _server = _fake_backplane()
    stream_id = uuid.uuid4().hex

    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)  # SUBSCRIBE + confirmation land

    await backplane.publish(stream_id, envelopes.start(stream_id, 0))
    await backplane.publish(stream_id, _done_pending(stream_id, 1))
    await backplane.publish(stream_id, _suggestions(stream_id, 2))

    received = await asyncio.wait_for(consumer, timeout=2.0)
    assert [e["type"] for e in received] == ["start", "done", "event"]
    assert received[-1]["name"] == "suggestions"
    await backplane.aclose()


async def test_pending_terminal_stops_at_grace_when_no_suggestions_come() -> None:
    """#489: a pending terminal with no trailing suggestions stops at grace expiry
    — the RedisBackplane subscribe must not hang the consumer."""
    server = _FakeServer()

    def factory() -> Any:
        client = _FakeClient(server)
        server.clients.append(client)
        return client

    backplane = RedisBackplane(
        "redis://unused/0", client_factory=factory, post_terminal_grace_seconds=0.05
    )
    stream_id = uuid.uuid4().hex

    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    await backplane.publish(stream_id, envelopes.start(stream_id, 0))
    await backplane.publish(stream_id, _done_pending(stream_id, 1))
    # No suggestions — the grace must expire and the subscriber return.

    received = await asyncio.wait_for(consumer, timeout=2.0)
    assert [e["type"] for e in received] == ["start", "done"]
    await backplane.aclose()


async def test_subscribe_does_not_close_the_pooled_publisher_client() -> None:
    """``subscribe`` owns a DEDICATED connection; its teardown must not

    take the pooled publisher client with it (a shared pubsub connection is
    impossible, so the two lifecycles stay separate).
    """
    backplane, server = _fake_backplane()
    stream_id = uuid.uuid4().hex

    await backplane.publish(stream_id, envelopes.start(stream_id, 0))
    await backplane.publish(stream_id, envelopes.done(stream_id, 1))
    pooled = server.clients[0]

    await asyncio.wait_for(_drain(backplane, stream_id), timeout=2.0)

    assert server.created == 2  # the publisher's pool + one subscriber connection
    assert server.clients[1].closed  # the subscriber's client IS torn down
    assert not pooled.closed  # the pooled publisher survives
    await backplane.aclose()


# --- AC-5: shutdown release + the #140 engine-per-loop rule -----------------


async def test_aclose_releases_the_pooled_client_and_a_later_publish_recreates_it() -> None:
    """AC-5: the pooled client is closed on request; the backplane stays usable."""
    backplane, server = _fake_backplane()
    stream_id = uuid.uuid4().hex

    await backplane.publish(stream_id, envelopes.start(stream_id, 0))
    first = server.clients[0]
    await backplane.aclose()

    assert first.closed
    await backplane.aclose()  # idempotent: closing twice is a no-op
    assert server.created == 1

    await backplane.publish(stream_id, envelopes.delta(stream_id, 1, {"text": "x"}))
    assert server.created == 2
    await backplane.aclose()


def test_a_client_built_on_a_dead_loop_is_never_reused() -> None:
    """AC-5 (#140): the lazy client is keyed by the RUNNING event loop.

    A warm Celery prefork worker runs each task on its OWN fresh loop
    (``tasks/runner.run_task``). A process-wide client cached without the loop
    key would be reused after its loop closed — the "attached to a different
    loop" failure the engine-per-loop fix (#140) exists to prevent.
    """
    backplane, server = _fake_backplane()
    stream_id = uuid.uuid4().hex

    async def _one_task_run(seq: int) -> None:
        await backplane.publish(stream_id, envelopes.delta(stream_id, seq, {"text": "x"}))

    asyncio.run(_one_task_run(0))
    asyncio.run(_one_task_run(1))

    # Two loops → two clients; the second run never touched the first's client.
    assert server.created == 2
    assert server.clients[0] is not server.clients[1]


async def test_lifespan_shutdown_releases_the_pooled_backplane_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-5: the app lifespan closes the process-wide backplane client.

    Mirrors the existing ``aclose_search_store`` / ``dispose_engine`` shutdown
    steps — without it the pooled client would outlive the serving loop.
    """
    backplane, server = _fake_backplane()
    monkeypatch.setattr(deps, "get_backplane", lambda: backplane)
    await backplane.publish("s1", envelopes.start("s1", 0))
    assert server.created == 1

    app = FastAPI()
    app.state.settings = get_settings()
    app.state.answer_tasks = set()
    with patch("app.main.get_object_store") as get_store:
        get_store.return_value.ensure_bucket = AsyncMock(return_value=None)
        async with lifespan(app):
            assert not server.clients[0].closed

    assert server.clients[0].closed


async def test_aclose_backplane_is_a_noop_for_a_non_redis_backplane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative: the offline in-memory backplane owns no client to release."""
    from app.realtime.backplane import InMemoryBackplane

    monkeypatch.setattr(deps, "get_backplane", InMemoryBackplane)
    await deps.aclose_backplane()  # must not raise
