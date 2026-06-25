"""In-memory backplane tests — pub/sub fan-out + the terminal lifecycle (CC-6 #24).

The :class:`InMemoryBackplane` is the offline stand-in for the Redis pub/sub
spine; it must satisfy the same contract the WS consumer + chat runtime rely on:
a subscriber receives every envelope published after it subscribes, in order, and
**stops after exactly one terminal** (``done``/``error``) envelope. The Redis
implementation is exercised only against a live stack (out of the offline suite).
"""

from __future__ import annotations

import asyncio

import pytest

from app.realtime import envelopes
from app.realtime.backplane import InMemoryBackplane, is_terminal


def test_is_terminal_recognises_done_and_error() -> None:
    assert is_terminal(envelopes.done("s", 1))
    assert is_terminal(envelopes.error("s", 1, {"title": "x", "status": 500}))
    assert not is_terminal(envelopes.start("s", 0))
    assert not is_terminal(envelopes.delta("s", 1, {"text": "hi"}))


async def _collect(backplane: InMemoryBackplane, stream_id: str) -> list[dict[str, object]]:
    received: list[dict[str, object]] = []
    async for env in backplane.subscribe(stream_id):
        received.append(env)
    return received


async def test_subscriber_receives_published_envelopes_in_order() -> None:
    backplane = InMemoryBackplane()
    consumer = asyncio.create_task(_collect(backplane, "s1"))
    await asyncio.sleep(0)  # let the subscriber register its queue

    await backplane.publish("s1", envelopes.start("s1", 0, data={"model": "m"}))
    await backplane.publish("s1", envelopes.delta("s1", 1, {"text": "he"}))
    await backplane.publish("s1", envelopes.delta("s1", 2, {"text": "llo"}))
    await backplane.publish("s1", envelopes.done("s1", 3, data={"messageId": "x"}))

    received = await asyncio.wait_for(consumer, timeout=2.0)
    assert [e["type"] for e in received] == ["start", "delta", "delta", "done"]
    assert [e["seq"] for e in received] == [0, 1, 2, 3]


async def test_subscriber_stops_after_terminal_error() -> None:
    backplane = InMemoryBackplane()
    consumer = asyncio.create_task(_collect(backplane, "s2"))
    await asyncio.sleep(0)

    await backplane.publish("s2", envelopes.start("s2", 0))
    await backplane.publish(
        "s2", envelopes.error("s2", 1, {"title": "Boom", "status": 503, "code": "x"})
    )
    # Anything after the terminal must not be delivered (the subscriber returned).
    await backplane.publish("s2", envelopes.delta("s2", 2, {"text": "late"}))

    received = await asyncio.wait_for(consumer, timeout=2.0)
    assert received[-1]["type"] == "error"
    assert "late" not in [str(e.get("data")) for e in received]


async def test_late_subscriber_receives_full_replay_after_producer_finished() -> None:
    """A subscriber connecting *after* the producer finished still gets every envelope.

    The realistic flow: the 202 schedules the producer, which can publish ``start``
    and a near-instant terminal *before* the WS client finishes connecting (authz →
    accept → subscribe). The backplane must replay the buffered envelopes — incl.
    the terminal — to that late subscriber. This is the late-subscriber race the
    Redis backplane had (#153, raw pub/sub has no buffering); InMemory locks the shared
    replay contract here, the live Redis test (``test_realtime_backplane_live.py``)
    is the regression for the production path.
    """
    backplane = InMemoryBackplane()
    # Producer runs to completion BEFORE anyone subscribes.
    await backplane.publish("late", envelopes.start("late", 0, data={"model": "m"}))
    await backplane.publish(
        "late", envelopes.error("late", 1, {"title": "Boom", "status": 503, "code": "x"})
    )

    received = await asyncio.wait_for(_collect(backplane, "late"), timeout=2.0)
    assert [e["type"] for e in received] == ["start", "error"]
    assert [e["seq"] for e in received] == [0, 1]
    # Exactly one terminal relayed (the contract's exactly-one-terminal rule).
    assert sum(1 for e in received if is_terminal(e)) == 1


async def test_publish_fans_out_to_multiple_subscribers() -> None:
    backplane = InMemoryBackplane()
    a = asyncio.create_task(_collect(backplane, "s3"))
    b = asyncio.create_task(_collect(backplane, "s3"))
    await asyncio.sleep(0)

    await backplane.publish("s3", envelopes.delta("s3", 0, {"text": "x"}))
    await backplane.publish("s3", envelopes.done("s3", 1))

    ra, rb = await asyncio.wait_for(asyncio.gather(a, b), timeout=2.0)
    assert [e["type"] for e in ra] == ["delta", "done"]
    assert [e["type"] for e in rb] == ["delta", "done"]


async def test_publish_to_unrelated_stream_is_not_received() -> None:
    backplane = InMemoryBackplane()
    consumer = asyncio.create_task(_collect(backplane, "mine"))
    await asyncio.sleep(0)

    await backplane.publish("other", envelopes.delta("other", 0, {"text": "nope"}))
    await backplane.publish("mine", envelopes.done("mine", 0))

    received = await asyncio.wait_for(consumer, timeout=2.0)
    assert [e["type"] for e in received] == ["done"]


async def test_aclose_unregisters_the_subscriber() -> None:
    backplane = InMemoryBackplane()
    sub = backplane.subscribe("s4")
    # Prime the generator so it registers its queue.
    task = asyncio.create_task(sub.__anext__())
    await asyncio.sleep(0)
    await backplane.publish("s4", envelopes.start("s4", 0))
    first = await asyncio.wait_for(task, timeout=2.0)
    assert first["type"] == "start"
    await sub.aclose()
    # After aclose the subscriber queue is gone; a later publish is a no-op (no
    # error, nothing buffered for a dropped subscriber).
    await backplane.publish("s4", envelopes.done("s4", 1))


@pytest.mark.parametrize("stream_id", ["a", "b", "c"])
async def test_streams_are_isolated(stream_id: str) -> None:
    backplane = InMemoryBackplane()
    consumer = asyncio.create_task(_collect(backplane, stream_id))
    await asyncio.sleep(0)
    await backplane.publish(stream_id, envelopes.done(stream_id, 0))
    received = await asyncio.wait_for(consumer, timeout=2.0)
    assert received[0]["streamId"] == stream_id
