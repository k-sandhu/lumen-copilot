"""``GET /ws/health`` — a heartbeat WebSocket that proves the transport.

On connect it emits a ``start`` envelope, then periodic ``delta`` "ping"
messages with a monotonically increasing ``seq`` under one ``streamId``, all per
``contracts/websocket-envelopes.schema.json``. This exists so the WS path and
the envelope contract are demonstrably alive from day one — it carries no
product data. Client disconnects are handled cleanly (the loop exits without
error). Product streams (chat tokens, citations) replace this pattern under
CC-6 / CC-11.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.logging import get_logger
from app.realtime import envelopes

router = APIRouter()
log = get_logger(__name__)

# Heartbeat cadence. Small enough to be visibly "alive", large enough to be quiet.
_PING_INTERVAL_SECONDS = 3.0


@router.websocket("/ws/health")
async def health_ws(websocket: WebSocket) -> None:
    """Accept the socket and stream heartbeat envelopes until disconnect."""
    await websocket.accept()
    stream_id = uuid.uuid4().hex
    seq = 0

    log.info("ws_health.open", stream_id=stream_id)
    try:
        await websocket.send_json(envelopes.start(stream_id, seq, data={"channel": "health"}))
        seq += 1

        while True:
            await asyncio.sleep(_PING_INTERVAL_SECONDS)
            await websocket.send_json(envelopes.delta(stream_id, seq, data={"ping": seq}))
            seq += 1
    except WebSocketDisconnect:
        # Normal client-initiated close — nothing to clean up beyond logging.
        log.info("ws_health.disconnect", stream_id=stream_id, last_seq=seq)
    except asyncio.CancelledError:
        # Server shutdown cancelled the task; let it propagate after logging.
        log.info("ws_health.cancelled", stream_id=stream_id, last_seq=seq)
        raise
