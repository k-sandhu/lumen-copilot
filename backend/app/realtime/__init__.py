"""Realtime transport — WebSocket handlers + Redis pub/sub backplane.

Single responsibility (ADR-0004 boundary table): own all WebSocket connections
and the Redis backplane that fans streamed output to the right connection
regardless of which backend instance holds it. **Nobody else may open a
WebSocket or publish/subscribe Redis.** Every WS message is an envelope per
``contracts/websocket-envelopes.schema.json`` (start -> delta|event* ->
done|error). This skeleton ships the ``/ws/health`` heartbeat that proves the
transport + envelope end-to-end; product streams (CC-6) mount here later.
"""
