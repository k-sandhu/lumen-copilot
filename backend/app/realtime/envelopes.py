"""WebSocket envelope builders.

In-code mirror of ``contracts/websocket-envelopes.schema.json``: every WS
message is an envelope discriminated by ``type`` with a ``streamId`` and a
monotonically increasing ``seq``. These helpers are the single place envelopes
are constructed so the wire shape stays in lockstep with the contract. Feature
payloads slot into ``data``/``name``/``problem`` per their cross-cutting; this
module stays payload-agnostic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def start(stream_id: str, seq: int, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a ``start`` envelope (stream-open metadata in ``data``)."""
    env: dict[str, Any] = {"type": "start", "streamId": stream_id, "seq": seq, "ts": _now_iso()}
    if data is not None:
        env["data"] = data
    return env


def delta(stream_id: str, seq: int, data: Any) -> dict[str, Any]:
    """Build a ``delta`` envelope (incremental output; ``data`` required)."""
    return {
        "type": "delta",
        "streamId": stream_id,
        "seq": seq,
        "ts": _now_iso(),
        "data": data,
    }


def event(stream_id: str, seq: int, name: str, data: Any = None) -> dict[str, Any]:
    """Build an ``event`` envelope (named side-band event; ``name`` required)."""
    env: dict[str, Any] = {
        "type": "event",
        "streamId": stream_id,
        "seq": seq,
        "ts": _now_iso(),
        "name": name,
    }
    if data is not None:
        env["data"] = data
    return env


def done(stream_id: str, seq: int, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a ``done`` terminal envelope (success summary in ``data``)."""
    env: dict[str, Any] = {"type": "done", "streamId": stream_id, "seq": seq, "ts": _now_iso()}
    if data is not None:
        env["data"] = data
    return env


def error(stream_id: str, seq: int, problem: dict[str, Any]) -> dict[str, Any]:
    """Build an ``error`` terminal envelope.

    ``problem`` must carry at least ``title`` and ``status`` (same model as the
    REST ``Problem`` shape).
    """
    return {
        "type": "error",
        "streamId": stream_id,
        "seq": seq,
        "ts": _now_iso(),
        "problem": problem,
    }
