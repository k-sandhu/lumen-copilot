"""Redis pub/sub backplane — the only Redis client owner (CC-6 #24).

Per the ADR-0004 boundary table, ``realtime/`` owns Redis. **Nobody else may
publish/subscribe Redis.** This module is the fan-out spine for streamed output:
the answer **producer** (the chat runtime, run as a background task off the send
handler) publishes the WS envelopes for a ``stream_id`` here, and the WS
**consumer** (the chat WS endpoint) subscribes to that ``stream_id`` and relays
them to the client. Producer and consumer are decoupled — they may even run in
different processes/instances — so the runtime is not bound to one socket.

Two implementations behind one :class:`Backplane` Protocol:

* :class:`RedisBackplane` — the production path, a thin wrapper over
  ``redis.asyncio`` pub/sub (the readiness :func:`ping` lives here too).
* :class:`InMemoryBackplane` — an in-process asyncio-queue fan-out used by the
  offline tests (and a single-process dev run), so the full streaming lifecycle
  is exercised without a running Redis.

Envelopes are JSON dicts (the ``contracts/websocket-envelopes.schema.json``
shapes). A subscriber yields them until it sees a **terminal** envelope
(``done`` / ``error``) — the exactly-one-terminal lifecycle — or the stream is
explicitly closed.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import redis.asyncio as aioredis

# Terminal envelope kinds: a subscriber stops after relaying one of these
# (exactly-one-terminal lifecycle, contracts/websocket-envelopes.schema.json).
_TERMINAL_TYPES = frozenset({"done", "error"})

# Channel namespace so chat streams never collide with other realtime traffic.
_CHANNEL_PREFIX = "chat:stream:"
# Key namespace for the stream→owner binding (separate from the pub/sub channel).
_OWNER_PREFIX = "chat:stream-owner:"
# How long the owner binding lives. Comfortably outstrips the gap between the 202
# (when the binding is written) and the WS connect that follows it, while bounding
# how long a (single-use) stream id stays authoritative. Matches the access-token
# ceiling (spec 0004 §2.3) — a stream cannot outlive the token that minted it.
_OWNER_TTL_SECONDS = 900


def _channel(stream_id: str) -> str:
    return f"{_CHANNEL_PREFIX}{stream_id}"


def _owner_key(stream_id: str) -> str:
    return f"{_OWNER_PREFIX}{stream_id}"


def is_terminal(envelope: dict[str, Any]) -> bool:
    """True iff ``envelope`` is a terminal (``done``/``error``) envelope."""
    return envelope.get("type") in _TERMINAL_TYPES


@dataclass(frozen=True, slots=True)
class StreamOwner:
    """The principal a stream is bound to — its asking owner + tenant.

    Written at the 202 (when the ``stream_id`` is minted) and read by the WS
    consumer before it subscribes, so an answer stream is only ever relayed to
    the user who asked the question (INV-1/INV-2, spec 0004 §2.1/§2.2).
    """

    owner_id: UUID
    tenant_id: UUID


@runtime_checkable
class Backplane(Protocol):
    """The pub/sub seam the chat runtime + WS endpoint share.

    The producer calls :meth:`publish` for each envelope of a ``stream_id``; the
    consumer iterates :meth:`subscribe` for that id and relays what it yields.
    Both sides depend on this Protocol, not a concrete client — so the in-memory
    fake and the real Redis backplane are interchangeable (testability + the
    ADR-0004 single-owner rule).
    """

    async def publish(self, stream_id: str, envelope: dict[str, Any]) -> None:
        """Publish one envelope to ``stream_id``'s channel."""
        ...

    def subscribe(self, stream_id: str) -> AsyncGenerator[dict[str, Any], None]:
        """Async-iterate envelopes for ``stream_id`` until a terminal one.

        Returned as an async generator so the consumer can ``aclose()`` it on
        disconnect (releasing the backplane connection deterministically).
        """
        ...

    async def bind_owner(self, stream_id: str, owner: StreamOwner) -> None:
        """Bind ``stream_id`` to the principal that may consume it (short TTL).

        Called at the 202 (mint time) so the WS consumer can later verify the
        connecting principal owns the stream before relaying a single envelope.
        """
        ...

    async def get_owner(self, stream_id: str) -> StreamOwner | None:
        """Return the principal ``stream_id`` is bound to, or ``None`` if unknown.

        ``None`` covers an unknown/expired id; the consumer must treat that
        identically to a mismatch (deny, existence non-disclosure).
        """
        ...


class RedisBackplane:
    """Redis-backed pub/sub fan-out (the production backplane)."""

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url

    async def publish(self, stream_id: str, envelope: dict[str, Any]) -> None:
        """Publish one envelope as JSON to ``stream_id``'s Redis channel."""
        client = aioredis.from_url(self._redis_url)  # type: ignore[no-untyped-call]
        try:
            await client.publish(_channel(stream_id), json.dumps(envelope))
        finally:
            await client.aclose()

    async def bind_owner(self, stream_id: str, owner: StreamOwner) -> None:
        """Persist ``stream_id``'s owner binding with a short TTL (``SETEX``)."""
        client = aioredis.from_url(self._redis_url)  # type: ignore[no-untyped-call]
        try:
            payload = json.dumps(
                {"owner_id": str(owner.owner_id), "tenant_id": str(owner.tenant_id)}
            )
            await client.set(_owner_key(stream_id), payload, ex=_OWNER_TTL_SECONDS)
        finally:
            await client.aclose()

    async def get_owner(self, stream_id: str) -> StreamOwner | None:
        """Read ``stream_id``'s owner binding, or ``None`` if absent/expired."""
        client = aioredis.from_url(self._redis_url)  # type: ignore[no-untyped-call]
        try:
            raw = await client.get(_owner_key(stream_id))
        finally:
            await client.aclose()
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return StreamOwner(owner_id=UUID(data["owner_id"]), tenant_id=UUID(data["tenant_id"]))

    async def subscribe(self, stream_id: str) -> AsyncGenerator[dict[str, Any], None]:
        """Subscribe to ``stream_id`` and yield envelopes until a terminal one.

        Opens a dedicated pub/sub connection, decodes each message JSON, and
        stops after relaying a ``done``/``error`` envelope. The connection is
        always closed in the ``finally`` (client disconnect / cancellation /
        terminal), so no Redis connection leaks.
        """
        client = aioredis.from_url(self._redis_url)  # type: ignore[no-untyped-call]
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe(_channel(stream_id))
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                raw = message.get("data")
                if raw is None:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                envelope = json.loads(raw)
                yield envelope
                if is_terminal(envelope):
                    return
        finally:
            await pubsub.unsubscribe(_channel(stream_id))
            await pubsub.aclose()
            await client.aclose()


class InMemoryBackplane:
    """In-process asyncio-queue fan-out (offline tests / single-process dev).

    A per-``stream_id`` set of subscriber queues; :meth:`publish` puts the
    envelope on each live queue. The same exactly-one-terminal contract holds: a
    subscriber yields until it sees a terminal envelope, then drops its queue.

    **Short replay buffer.** Unlike raw Redis pub/sub, this keeps a bounded
    replay of each stream's envelopes (including a completed one's terminal) so a
    subscriber that joins *after* the producer started — even after it finished —
    still receives the full stream. That is the realistic flow (a client connects
    to the WS right after the 202) and removes test-ordering flakiness; the buffer
    is capped per stream and a fresh stream uses a fresh id. Production uses
    :class:`RedisBackplane`.

    This is **not** cross-process — it exists so the streaming lifecycle is
    testable without Redis.
    """

    # Cap so a never-consumed stream cannot grow unbounded in memory.
    _MAX_REPLAY = 512

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._replay: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._owners: dict[str, StreamOwner] = {}

    async def publish(self, stream_id: str, envelope: dict[str, Any]) -> None:
        buf = self._replay[stream_id]
        buf.append(envelope)
        if len(buf) > self._MAX_REPLAY:
            del buf[0 : len(buf) - self._MAX_REPLAY]
        for queue in list(self._subscribers.get(stream_id, ())):
            queue.put_nowait(envelope)

    async def bind_owner(self, stream_id: str, owner: StreamOwner) -> None:
        self._owners[stream_id] = owner

    async def get_owner(self, stream_id: str) -> StreamOwner | None:
        return self._owners.get(stream_id)

    async def subscribe(self, stream_id: str) -> AsyncGenerator[dict[str, Any], None]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # Replay anything already published for this stream so a late subscriber
        # does not miss the opening envelopes, then receive live ones.
        for envelope in list(self._replay.get(stream_id, ())):
            queue.put_nowait(envelope)
        self._subscribers[stream_id].add(queue)
        try:
            while True:
                envelope = await queue.get()
                yield envelope
                if is_terminal(envelope):
                    return
        finally:
            subs = self._subscribers.get(stream_id)
            if subs is not None:
                subs.discard(queue)
                if not subs:
                    self._subscribers.pop(stream_id, None)


async def ping(redis_url: str) -> None:
    """Open a transient async client and ``PING`` Redis, then close it."""
    client = aioredis.from_url(redis_url)  # type: ignore[no-untyped-call]  # redis from_url is untyped
    try:
        await client.ping()
    finally:
        await client.aclose()
