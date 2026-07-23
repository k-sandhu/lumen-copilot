"""Redis pub/sub backplane — the only Redis client owner (CC-6 #24).

Per the ADR-0004 boundary table, ``realtime/`` owns Redis. **Nobody else may
publish/subscribe Redis.** This module is the fan-out spine for streamed output:
the answer **producer** (the chat runtime, run as a background task off the send
handler) publishes the WS envelopes for a ``stream_id`` here, and the WS
**consumer** (the chat WS endpoint) subscribes to that ``stream_id`` and relays
them to the client. Producer and consumer are decoupled — they may even run in
different processes/instances — so the runtime is not bound to one socket.

Two implementations behind one :class:`Backplane` Protocol:

* :class:`RedisBackplane` — the production path over ``redis.asyncio`` pub/sub,
  with a bounded per-stream **replay** list so a subscriber that connects *after*
  the producer started still receives the envelopes it would otherwise miss (raw
  pub/sub has no buffering; the readiness :func:`ping` lives here too).
* :class:`InMemoryBackplane` — an in-process asyncio-queue fan-out used by the
  offline tests (and a single-process dev run), so the full streaming lifecycle
  is exercised without a running Redis.

Both implementations replay so a late subscriber is never silently empty: the
202 schedules the producer, which may publish ``start`` and even a near-instant
terminal *before* the WS client finishes connecting (authz → ``accept`` →
``subscribe``). A subscriber drains the replay before relaying live messages.

Envelopes are JSON dicts (the ``contracts/websocket-envelopes.schema.json``
shapes). A subscriber yields them until it sees a **terminal** envelope
(``done`` / ``error``) — the exactly-one-terminal lifecycle — or the stream is
explicitly closed.

**Connection ownership (issue #487).** :class:`RedisBackplane` keeps ONE pooled
client per event loop for the command path (``publish`` / ``bind_owner`` /
``get_owner``) and gives :meth:`RedisBackplane.subscribe` its own dedicated
pub/sub connection. The pooled client is released by
:meth:`RedisBackplane.aclose`, wired into the app lifespan via
:func:`app.api.deps.aclose_backplane`. Producer-side *batching* is not done here:
coalescing lives at the envelope-construction seam in the chat runtime, so the
replay list stores byte-identical envelopes to what the live channel delivers
(the #153 replay/live equivalence).
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncGenerator, Callable
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

# Key namespace for the per-stream replay list — separate from the pub/sub channel
# and the owner binding. The producer mirrors every envelope here so a subscriber
# that connects after publishing started can drain the head it missed.
_REPLAY_PREFIX = "chat:stream-replay:"
# Max envelopes retained per stream for replay — shared by both backplanes so a
# late subscriber sees the same bounded history regardless of implementation. A
# fresh stream uses a fresh id, so this only ever caps a single answer's output.
_MAX_REPLAY = 512
# The replay list must live at least as long as the owner binding that gates the
# connect: if a caller is still allowed to subscribe, the stream must still be
# replayable. Refreshed on every publish, so a long stream never expires mid-flight.
_REPLAY_TTL_SECONDS = _OWNER_TTL_SECONDS
# Redis sends a subscribe confirmation as the *first* message on a pub/sub
# connection. Draining it proves the subscription is registered server-side before
# we read the replay list (a separate connection), so an envelope published during
# the handshake cannot slip past both paths. Bounded so a silent Redis can't hang.
_SUBSCRIBE_CONFIRM_TYPES = frozenset({"subscribe", "psubscribe"})
_SUBSCRIBE_CONFIRM_TIMEOUT_SECONDS = 5.0

# How a Redis client is constructed (issue #487). ``redis.asyncio``'s client is
# not usefully typed for the paths used here (``from_url`` is untyped and the
# command returns are sync/async unions), so the client itself is held as ``Any``
# at this one boundary — the module's public surface stays fully typed. Injecting
# a factory is also what lets the lifecycle tests run without a live Redis.
RedisClientFactory = Callable[[], Any]


def _channel(stream_id: str) -> str:
    return f"{_CHANNEL_PREFIX}{stream_id}"


def _owner_key(stream_id: str) -> str:
    return f"{_OWNER_PREFIX}{stream_id}"


def _replay_key(stream_id: str) -> str:
    return f"{_REPLAY_PREFIX}{stream_id}"


def _loads(raw: Any) -> dict[str, Any]:
    """Decode a Redis payload (``bytes`` or ``str``) into an envelope dict."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    envelope: dict[str, Any] = json.loads(raw)
    return envelope


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
    """Redis-backed pub/sub fan-out (the production backplane).

    **One pooled client per event loop** (issue #487). ``publish`` /
    ``bind_owner`` / ``get_owner`` share a single lazily-created
    ``redis.asyncio`` client instead of opening and tearing one down per call —
    a streamed answer used to pay a TCP connect (plus any auth/TLS handshake)
    and a close for *every* envelope, serialised into streaming throughput.
    ``subscribe`` still takes its own dedicated connection: a pub/sub connection
    cannot be shared with ordinary commands, and its lifetime is the
    subscription's, not the process's.

    The lazy client is keyed by the **running event loop**, so a client whose
    connections were opened on a now-closed loop is never reused — the #140
    engine-per-loop failure mode. That is what keeps the class Celery-safe: a
    warm prefork worker runs each task on a fresh loop (``tasks/runner.run_task``)
    and simply gets its own client for that run, exactly as ``search/store.py``
    builds a per-run store. The API process has one serving loop, so it creates
    one client, released by :meth:`aclose` from the app lifespan.

    ``client_factory`` is the injectable construction seam: production leaves it
    ``None`` (``redis.asyncio.from_url``); lifecycle/failure tests inject a fake
    so they need no live Redis.
    """

    def __init__(
        self,
        redis_url: str,
        *,
        client_factory: RedisClientFactory | None = None,
    ) -> None:
        self._redis_url = redis_url
        self._client_factory: RedisClientFactory = client_factory or self._connect
        # The pooled client and the loop it was built on (``None`` until first
        # use — construction stays I/O-free and loop-free, so the process-wide
        # singleton in ``api/deps.py`` can be built at import time).
        self._pool: Any | None = None
        self._pool_loop: asyncio.AbstractEventLoop | None = None

    def _connect(self) -> Any:
        """Open a new client for this backplane's URL (the default factory)."""
        return aioredis.from_url(self._redis_url)  # type: ignore[no-untyped-call]

    def _client(self) -> Any:
        """The pooled client for the RUNNING loop, created on first use.

        Keyed on the running loop so a client built on another (by then dead)
        loop is never handed out — reusing one is the "attached to a different
        loop" failure the #140 engine-per-loop fix exists to prevent. A stale
        client is dropped rather than closed: its sockets died with its loop, and
        awaiting its close from *this* loop would touch a foreign one.
        """
        loop = asyncio.get_running_loop()
        if self._pool is None or self._pool_loop is not loop:
            self._pool = self._client_factory()
            self._pool_loop = loop
        return self._pool

    async def aclose(self) -> None:
        """Release the pooled client (app lifespan shutdown; idempotent).

        A no-op when nothing was ever published (the offline tests, or a process
        that only ever subscribed), so create *and* close stay on one event loop
        — the same hygiene :func:`app.search.aclose_search_store` follows. The
        backplane stays usable afterwards: the next call re-creates the client.
        """
        client, loop = self._pool, self._pool_loop
        self._pool = None
        self._pool_loop = None
        if client is None:
            return
        if loop is not None and loop is not asyncio.get_running_loop():
            # Built on another loop (a finished Celery task's): its connections
            # went with it and closing from here would touch a foreign loop.
            return
        await client.aclose()

    async def publish(self, stream_id: str, envelope: dict[str, Any]) -> None:
        """Publish one envelope — live fan-out **and** bounded replay.

        The envelope is ``PUBLISH``ed for any already-connected subscriber *and*
        appended to a capped, TTL'd replay list. Raw Redis pub/sub has no
        buffering, so a subscriber that connects after the producer started — the
        realistic ``202`` → WS-connect flow, and guaranteed for a fast/instant
        stream — would otherwise miss every envelope. The replay list is the Redis
        analogue of :class:`InMemoryBackplane`'s buffer; a new subscriber drains it
        before going live, closing the late-subscriber race (#153).

        ``RPUSH`` is ordered before ``PUBLISH`` on one pipeline, so the envelope is
        durable in the list no later than it is delivered live. Combined with the
        consumer's subscribe-then-replay order, every envelope arrives via the
        replay, the live channel, or both — never neither.

        Runs on the **pooled** client (#487): the commands are the same, only the
        connection is now reused across the answer instead of rebuilt per envelope.
        """
        client = self._client()
        payload = json.dumps(envelope)
        replay_key = _replay_key(stream_id)
        async with client.pipeline(transaction=False) as pipe:
            pipe.rpush(replay_key, payload)
            pipe.ltrim(replay_key, -_MAX_REPLAY, -1)
            pipe.expire(replay_key, _REPLAY_TTL_SECONDS)
            pipe.publish(_channel(stream_id), payload)
            await pipe.execute()

    async def bind_owner(self, stream_id: str, owner: StreamOwner) -> None:
        """Persist ``stream_id``'s owner binding with a short TTL (``SETEX``).

        On the pooled client (#487 / #492 AC-3) — this runs inside the ``202``
        request, so a per-call connect/teardown sat directly on the send path.
        """
        payload = json.dumps({"owner_id": str(owner.owner_id), "tenant_id": str(owner.tenant_id)})
        await self._client().set(_owner_key(stream_id), payload, ex=_OWNER_TTL_SECONDS)

    async def get_owner(self, stream_id: str) -> StreamOwner | None:
        """Read ``stream_id``'s owner binding, or ``None`` if absent/expired."""
        raw = await self._client().get(_owner_key(stream_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return StreamOwner(owner_id=UUID(data["owner_id"]), tenant_id=UUID(data["tenant_id"]))

    async def subscribe(self, stream_id: str) -> AsyncGenerator[dict[str, Any], None]:
        """Subscribe to ``stream_id`` and yield envelopes until a terminal one.

        Order matters for the late-subscriber fix: (1) ``SUBSCRIBE`` to the live
        channel and wait for Redis to confirm it, (2) replay the buffered list,
        then (3) relay live messages — skipping any whose ``seq`` the replay
        already delivered (an envelope published during the handshake can land in
        both). Stops after relaying one ``done``/``error`` envelope, *including* a
        terminal already sitting in the replay (the producer finished before we
        connected — the exact case raw pub/sub dropped). The pub/sub connection is
        always closed in the ``finally`` (disconnect / cancellation / terminal), so
        no Redis connection leaks.

        Deliberately **not** on the pooled publisher client (#487): a pub/sub
        connection cannot carry ordinary commands, and this one's lifetime is the
        subscription's — closing it here must never take the process-wide
        publisher down with it. One connection serves the whole subscription, so
        the consumer side was already O(1) and is unchanged.
        """
        client = self._client_factory()
        pubsub = client.pubsub()
        channel = _channel(stream_id)
        try:
            await pubsub.subscribe(channel)
            # Confirm the subscription is registered server-side before reading the
            # replay list (a separate connection): otherwise an envelope published
            # in the gap could be absent from the list *and* missed live.
            await self._await_subscribe_confirmed(pubsub)

            # Replay the head the producer published before we connected (incl. a
            # terminal if it already finished). Track the high-water seq so the
            # live loop relays each envelope exactly once.
            replayed_seq = -1
            for raw in await client.lrange(_replay_key(stream_id), 0, -1):
                envelope = _loads(raw)
                seq = envelope.get("seq")
                if isinstance(seq, int):
                    replayed_seq = max(replayed_seq, seq)
                yield envelope
                if is_terminal(envelope):
                    return

            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                raw = message.get("data")
                if raw is None:
                    continue
                envelope = _loads(raw)
                seq = envelope.get("seq")
                # Drop anything the replay already delivered (seq is monotonic per
                # stream), so an envelope in both the list and the live channel is
                # relayed once.
                if isinstance(seq, int) and seq <= replayed_seq:
                    continue
                yield envelope
                if is_terminal(envelope):
                    return
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
            await client.aclose()

    @staticmethod
    async def _await_subscribe_confirmed(pubsub: Any) -> None:
        """Block until Redis acknowledges the ``SUBSCRIBE`` (or a bounded timeout).

        The acknowledgement is the first message on a fresh pub/sub connection, so
        a single drained message proves the subscription is live. On timeout we
        proceed best-effort rather than hang a silent connection forever.
        """
        while True:
            message = await pubsub.get_message(timeout=_SUBSCRIBE_CONFIRM_TIMEOUT_SECONDS)
            if message is None:
                return
            if message.get("type") in _SUBSCRIBE_CONFIRM_TYPES:
                return


class InMemoryBackplane:
    """In-process asyncio-queue fan-out (offline tests / single-process dev).

    A per-``stream_id`` set of subscriber queues; :meth:`publish` puts the
    envelope on each live queue. The same exactly-one-terminal contract holds: a
    subscriber yields until it sees a terminal envelope, then drops its queue.

    **Short replay buffer.** Like :class:`RedisBackplane` (which mirrors envelopes
    into a capped Redis list), this keeps a bounded in-process replay of each
    stream's envelopes (including a completed one's terminal) so a subscriber that
    joins *after* the producer started — even after it finished — still receives
    the full stream. That is the realistic flow (a client connects to the WS right
    after the 202) and removes test-ordering flakiness; the buffer is capped
    (``_MAX_REPLAY``, shared with the Redis path) per stream and a fresh stream
    uses a fresh id. Production uses :class:`RedisBackplane`.

    This is **not** cross-process — it exists so the streaming lifecycle is
    testable without Redis.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._replay: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._owners: dict[str, StreamOwner] = {}

    async def publish(self, stream_id: str, envelope: dict[str, Any]) -> None:
        buf = self._replay[stream_id]
        buf.append(envelope)
        if len(buf) > _MAX_REPLAY:
            del buf[0 : len(buf) - _MAX_REPLAY]
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
