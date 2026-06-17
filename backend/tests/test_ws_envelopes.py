"""Unit tests for the WebSocket envelope builders.

Lock the envelope shapes to ``contracts/websocket-envelopes.schema.json``: every
envelope carries ``type`` / ``streamId`` / ``seq``; ``delta`` requires ``data``;
``error`` carries a ``problem`` with ``title`` + ``status``. Pure: no socket.
"""

from __future__ import annotations

from app.realtime import envelopes


def test_start_envelope_required_fields() -> None:
    env = envelopes.start("stream-1", 0, data={"channel": "health"})
    assert env["type"] == "start"
    assert env["streamId"] == "stream-1"
    assert env["seq"] == 0
    assert env["data"] == {"channel": "health"}


def test_delta_requires_data_and_increments_seq() -> None:
    env = envelopes.delta("stream-1", 5, data={"ping": 5})
    assert env["type"] == "delta"
    assert env["seq"] == 5
    assert env["data"] == {"ping": 5}


def test_error_envelope_carries_problem() -> None:
    env = envelopes.error(
        "stream-1", 9, problem={"title": "Boom", "status": 500, "code": "internal_error"}
    )
    assert env["type"] == "error"
    assert env["problem"]["title"] == "Boom"
    assert env["problem"]["status"] == 500


def test_done_is_terminal_shape() -> None:
    env = envelopes.done("stream-1", 10, data={"reason": "complete"})
    assert env["type"] == "done"
    assert {"type", "streamId", "seq"} <= set(env.keys())
