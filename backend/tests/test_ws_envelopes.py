"""Unit tests for the WebSocket envelope builders.

Lock the envelope shapes to ``contracts/websocket-envelopes.schema.json``: every
envelope carries ``type`` / ``streamId`` / ``seq``; ``delta`` requires ``data``;
``error`` carries a ``problem`` with ``title`` + ``status``. Pure: no socket.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.realtime import envelopes


def _ws_schema() -> dict[str, Any]:
    """The canonical WS envelope contract (``contracts/websocket-envelopes.schema.json``)."""
    root = Path(__file__).resolve().parent.parent.parent
    return json.loads((root / "contracts" / "websocket-envelopes.schema.json").read_text())


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


def test_answer_retract_validates_and_rejects_a_data_payload() -> None:
    """BE-6 (#488): ``answer_retract`` carries NO data — the schema now enforces an
    EXACT shape for it. The runtime's bare retraction validates; a retraction with a
    junk ``data`` payload, or any unexpected top-level property, is rejected (before
    the tightening the generic ``Event.data: {}`` accepted any payload). The
    tightening is scoped to answer_retract: other event names still carry data."""
    import jsonschema

    schema = _ws_schema()

    # The runtime's exact wire shape (envelopes.event with no data) validates.
    valid = envelopes.event("s", 3, name="answer_retract")
    assert "data" not in valid
    jsonschema.validate(valid, schema)

    # A retraction carrying a junk data payload is rejected...
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**valid, "data": {"tampered": True}}, schema)

    # ...even an empty-object data payload (answer_retract carries none at all)...
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**valid, "data": {}}, schema)

    # ...as is an unexpected top-level property on a retraction.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**valid, "surprise": 1}, schema)

    # Regression: a DIFFERENT event name that legitimately carries data still
    # validates — the tightening must not break the other event names.
    narration = envelopes.event("s", 4, name="narration", data={"text": "hi", "turn": 1})
    jsonschema.validate(narration, schema)
