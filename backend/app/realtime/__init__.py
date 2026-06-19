"""Realtime transport — WebSocket handlers + Redis pub/sub backplane.

Single responsibility (ADR-0004 boundary table): own all WebSocket connections
and the Redis backplane that fans streamed output to the right connection
regardless of which backend instance holds it. **Nobody else may open a
WebSocket or publish/subscribe Redis.** Every WS message is an envelope per
``contracts/websocket-envelopes.schema.json`` (start -> delta|event* ->
done|error). Ships the ``/ws/health`` heartbeat (proves the transport) and the
``/ws/chat/{stream_id}`` answer-stream consumer (CC-6 #24 / CC-11 #26), fanned
out through the :mod:`app.realtime.backplane` pub/sub spine.
"""

from app.realtime.backplane import (
    Backplane,
    InMemoryBackplane,
    RedisBackplane,
    is_terminal,
)

__all__ = [
    "Backplane",
    "InMemoryBackplane",
    "RedisBackplane",
    "is_terminal",
]
