"""Live Redis backplane tests — the late-subscriber race regression (#153, CC-6 #24).

The offline suite exercises :class:`InMemoryBackplane`, whose replay buffer
*masks* the production bug: raw Redis pub/sub has no buffering, so a subscriber
that connects after a fast producer finishes receives **zero** envelopes. These
tests pin the fix against a **real** Redis — they are the regression for
:class:`RedisBackplane`'s bounded replay list.

They open a real socket, so they are gated twice and skipped by default (issue
#94 parity): an explicit ``RUN_LIVE=1`` opt-in, plus Redis reachability checked
in the fixture (which ``pytest.skip``s if the server is down). The default
offline ``uv run pytest`` therefore opens no external socket. Each test uses a
fresh ``uuid4`` stream id and deletes its keys in teardown so a live Redis is
left clean.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest

from app.core.config import Settings
from app.realtime import envelopes
from app.realtime.backplane import (
    RedisBackplane,
    _replay_key,
    is_terminal,
    ping,
)

_RUN_LIVE = os.environ.get("RUN_LIVE", "").strip() not in ("", "0", "false", "False")

live = pytest.mark.skipif(
    not _RUN_LIVE,
    reason=(
        "live Redis backplane test opted out: needs RUN_LIVE=1 AND a reachable "
        f"Redis (RUN_LIVE={'set' if _RUN_LIVE else 'unset'}) — skipped (offline-safe)."
    ),
)


@pytest.fixture
async def _redis_url() -> AsyncIterator[str]:
    """The configured Redis URL, skipping the test if Redis is unreachable.

    Reachability is probed with :func:`ping` (a transient client, closed in the
    helper) so an opted-in run against a down Redis skips cleanly rather than
    erroring — and an offline run never reaches here (gated by ``RUN_LIVE``).
    """
    settings = Settings()  # reads real env / .env (pydantic-settings)
    url = settings.redis_url
    try:
        await ping(url)
    except Exception as exc:  # noqa: BLE001 — any connection failure → skip, not error
        pytest.skip(f"Redis unreachable at {url!r}: {type(exc).__name__}")
    yield url


async def _drain(backplane: RedisBackplane, stream_id: str) -> list[dict[str, object]]:
    received: list[dict[str, object]] = []
    async for envelope in backplane.subscribe(stream_id):
        received.append(envelope)
    return received


async def _delete_replay(redis_url: str, stream_id: str) -> None:
    """Best-effort teardown of a stream's replay list (TTL would also reap it)."""
    import redis.asyncio as aioredis

    client = aioredis.from_url(redis_url)  # type: ignore[no-untyped-call]
    try:
        await client.delete(_replay_key(stream_id))
    finally:
        await client.aclose()


@live
@pytest.mark.live
async def test_late_subscriber_gets_full_stream_when_producer_already_finished(
    _redis_url: str,
) -> None:
    """The bug, reproduced: publish start + terminal, THEN subscribe — must get both.

    This is exactly the path that previously hung: a WS client that connects after
    the 202 once the producer has already published ``start`` (seq 0) and a
    near-instant terminal (seq 1). Without the replay list the subscriber receives
    nothing and blocks until its own read timeout; with it, the buffered head —
    incl. the terminal — is replayed and the subscriber returns immediately.
    """
    backplane = RedisBackplane(_redis_url)
    stream_id = f"itest-late-{uuid.uuid4()}"
    try:
        await backplane.publish(stream_id, envelopes.start(stream_id, 0, data={"model": "m"}))
        await backplane.publish(
            stream_id,
            envelopes.error(stream_id, 1, {"title": "Boom", "status": 503, "code": "x"}),
        )

        # The timeout is the regression guard: pre-fix this blocks forever.
        received = await asyncio.wait_for(_drain(backplane, stream_id), timeout=5.0)

        assert [e["type"] for e in received] == ["start", "error"]
        assert [e["seq"] for e in received] == [0, 1]
        assert sum(1 for e in received if is_terminal(e)) == 1
    finally:
        await _delete_replay(_redis_url, stream_id)


@live
@pytest.mark.live
async def test_subscriber_connected_before_publish_still_receives_live(
    _redis_url: str,
) -> None:
    """The happy path is intact: a subscriber connected first still gets live envelopes.

    Guards against the replay change breaking normal streaming — here the replay is
    empty at subscribe time and every envelope arrives over the live channel, in
    order, stopping after the terminal.
    """
    backplane = RedisBackplane(_redis_url)
    stream_id = f"itest-live-{uuid.uuid4()}"
    try:
        consumer = asyncio.create_task(_drain(backplane, stream_id))
        # Give the subscriber time to SUBSCRIBE + confirm + drain the empty replay.
        await asyncio.sleep(0.3)

        await backplane.publish(stream_id, envelopes.start(stream_id, 0))
        await backplane.publish(stream_id, envelopes.delta(stream_id, 1, {"text": "hi"}))
        await backplane.publish(stream_id, envelopes.done(stream_id, 2))

        received = await asyncio.wait_for(consumer, timeout=5.0)
        assert [e["type"] for e in received] == ["start", "delta", "done"]
        assert [e["seq"] for e in received] == [0, 1, 2]
    finally:
        await _delete_replay(_redis_url, stream_id)
