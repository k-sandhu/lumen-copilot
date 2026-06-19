"""``GET /ws/chat/{stream_id}`` — the chat answer stream consumer (CC-6 #24).

The WebSocket side of the answer path. A client that received a ``stream_id``
from ``POST /chat/sessions/{id}/messages`` (202) connects here to consume the
assistant answer as the contract envelope sequence:

    start(ChatStartData)
      → ( delta(ChatTokenDelta) | event:tool_call | event:tool_result
          | event:citation )*
      → done(ChatDoneData) | error(Problem)

This endpoint is a thin **consumer**: it authenticates, subscribes to the
``realtime/`` backplane for the ``stream_id``, and relays whatever the
**producer** (the chat runtime, running off the send handler) publishes. Producer
and consumer are fully decoupled through the backplane (Redis in production, an
in-memory fake offline) — the consumer holds no business logic and never touches
the model, retrieval, or the DB (ADR-0004 boundaries).

**Auth.** A browser WebSocket cannot send an ``Authorization`` header, so the
access token rides the ``token`` query param and is validated through ``auth/``
(the only token validator) before the socket is accepted; an invalid/missing
token closes the socket with a policy-violation code.

**Lifecycle / cancellation.** Relaying stops after exactly one terminal envelope
(``done``/``error``) — the contract's exactly-one-terminal rule — or when the
client disconnects (the subscription is closed in ``finally``, releasing the
backplane connection; no task or connection leaks).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.api.deps import get_backplane, get_settings_dep
from app.auth import InvalidTokenError, verify_access_token
from app.core.logging import get_logger
from app.realtime.backplane import is_terminal

router = APIRouter()
log = get_logger(__name__)

# RFC 6455 close codes used here.
_WS_POLICY_VIOLATION = 1008  # auth failure: the token was missing/invalid.


@router.websocket("/ws/chat/{stream_id}")
async def chat_ws(
    websocket: WebSocket,
    stream_id: str,
    token: str = Query(default=""),
) -> None:
    """Authenticate, then relay the answer stream for ``stream_id`` to the client.

    The token is validated *before* ``accept`` so an unauthenticated client never
    establishes the socket (INV-4). On success the endpoint subscribes to the
    backplane and forwards each envelope verbatim until a terminal one arrives or
    the client goes away.
    """
    settings = get_settings_dep()
    try:
        verify_access_token(token, settings)
    except InvalidTokenError:
        # Reject before accept: close with a policy-violation code (no envelope).
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return

    await websocket.accept()
    backplane = get_backplane()
    log.info("ws_chat.open", stream_id=stream_id)

    subscription = backplane.subscribe(stream_id)
    try:
        async for envelope in subscription:
            await websocket.send_json(envelope)
            if is_terminal(envelope):
                log.info("ws_chat.terminal", stream_id=stream_id, terminal=envelope.get("type"))
                break
    except WebSocketDisconnect:
        # Client closed mid-stream — stop relaying (the producer is decoupled and
        # continues/finishes independently; cancellation of generation is handled
        # by the runtime task).
        log.info("ws_chat.disconnect", stream_id=stream_id)
    finally:
        await subscription.aclose()
