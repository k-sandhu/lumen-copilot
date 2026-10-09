"""``GET /ws/chat/{stream_id}`` — the chat answer stream consumer (CC-6 #24).

The WebSocket side of the answer path. A client that received a ``stream_id``
from ``POST /chat/sessions/{id}/messages`` (202) connects here to consume the
assistant answer as the contract envelope sequence:

    start(ChatStartData)
      → ( delta(ChatTokenDelta) | event:tool_call | event:tool_result
          | event:citation )*
      → done(ChatDoneData) | error(Problem)

This endpoint is a thin **consumer**: it authenticates, **authorizes** (the
stream must belong to the connecting principal), subscribes to the ``realtime/``
backplane for the ``stream_id``, and relays whatever the **producer** (the chat
runtime, running off the send handler) publishes. Producer and consumer are fully
decoupled through the backplane (Redis in production, an in-memory fake offline) —
the consumer holds no business logic and never touches the model, retrieval, or
the DB (ADR-0004 boundaries).

**Auth.** A browser WebSocket cannot send an ``Authorization`` header, so the
access token rides the ``access_token`` query param (matching the frontend WS
client, ``frontend/src/api/ws.ts``) and is validated through ``auth/`` (the only
token validator) **before** the socket is accepted; an invalid/missing token is
denied pre-accept (INV-4). Because the close happens before ``accept``, the
client never sees a ``1008`` close frame — Starlette rejects the WebSocket
handshake itself, which the client observes as an HTTP **403** upgrade failure.

**Authz (INV-1/INV-2).** A bare random ``stream_id`` carries no identity, so a
valid token alone is *not* enough: every stream is bound to the asking principal
at the 202 (``backplane.bind_owner``). This consumer looks that binding up and
relays only if the connecting principal's ``user_id`` **and** ``tenant_id`` match.
An unknown stream and a foreign/cross-tenant one are denied **identically** —
both close the socket pre-accept via the same code path and **no envelope is
emitted** — so a caller cannot learn whether someone else's stream exists
(existence non-disclosure, spec 0004 §2.1). As above, the pre-accept close means
the client sees an HTTP 403 handshake rejection, not a ``1008`` close frame.

**Lifecycle / cancellation.** Relaying stops after exactly one terminal envelope
(``done``/``error``) — the contract's exactly-one-terminal rule — or when the
client disconnects (the subscription is closed in ``finally``, releasing the
backplane connection; no task or connection leaks).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from app.api.deps import AuditSinkFactory, get_backplane, get_settings_dep
from app.auth import InvalidTokenError, Principal, verify_access_token
from app.core.logging import get_logger
from app.realtime.backplane import is_terminal
from app.services.chat_stream_access import ChatStreamAccessService

router = APIRouter()
log = get_logger(__name__)

# The RFC 6455 close code intended for every policy denial (missing/invalid token
# *and* a stream not owned by the caller) so the two are indistinguishable to the
# client (existence non-disclosure, spec 0004 §2.1). Note: both denials close the
# socket **before** ``accept`` (see ``chat_ws``), and Starlette turns a pre-accept
# close into an HTTP 403 rejection of the WebSocket handshake — so no ``1008``
# close frame actually reaches the client. The constant documents the intended
# code and is the one that would be sent were the close ever to occur post-accept.
_WS_POLICY_VIOLATION = 1008


async def verified_stream_principal(
    websocket: WebSocket,
    access_token: str = Query(default=""),
) -> Principal | None:
    """Authenticate through auth/ before accepting or creating actor attribution."""
    try:
        return verify_access_token(access_token, get_settings_dep())
    except InvalidTokenError:
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return None


async def stream_access_service(
    websocket: WebSocket,
    principal: Annotated[Principal | None, Depends(verified_stream_principal)],
    make_audit_sink: AuditSinkFactory,
) -> ChatStreamAccessService | None:
    if principal is None:
        return None
    return ChatStreamAccessService(
        backplane=get_backplane(),
        principal=principal,
        denials=make_audit_sink.denial_context(
            principal.tenant_id,
            principal=principal,
            request_id=websocket.headers.get("x-request-id") or uuid.uuid4().hex,
            source_ip=websocket.client.host if websocket.client else "unknown",
        ),
    )


@router.websocket("/ws/chat/{stream_id}")
async def chat_ws(
    websocket: WebSocket,
    stream_id: str,
    access: Annotated[ChatStreamAccessService | None, Depends(stream_access_service)],
) -> None:
    """Authenticate + authorize, then relay the answer stream for ``stream_id``.

    The token is validated *before* ``accept`` so an unauthenticated client never
    establishes the socket (INV-4). The principal is then matched against the
    stream's owner binding (INV-1/INV-2): an unknown id or a foreign/cross-tenant
    principal is denied identically (close, no envelope). Every denial here closes
    before ``accept``, so the handshake is rejected — the client observes an HTTP
    403 upgrade failure and no ``1008`` close frame and no envelope ever reach it.
    Only on a clean match does the endpoint subscribe to the backplane and forward
    each envelope verbatim until a terminal one arrives or the client goes away.
    """
    if access is None:
        return

    backplane = get_backplane()
    # Authorize the *stream* to this principal before accepting: a valid token is
    # not enough — the stream id alone confers no access (INV-1/INV-2). An unknown
    # id and a foreign/cross-tenant one are treated the same (deny, no envelope,
    # existence non-disclosure). Logged without revealing whether the id existed.
    if not await access.authorize(stream_id):
        log.info("ws_chat.denied", stream_id=stream_id)
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return

    await websocket.accept()
    log.info("ws_chat.open", stream_id=stream_id)

    subscription = backplane.subscribe(stream_id)
    try:
        async for envelope in subscription:
            await websocket.send_json(envelope)
            if is_terminal(envelope):
                # Relay the terminal, but do NOT break: the backplane may still
                # relay one post-terminal ``event:suggestions`` for a
                # ``done(pendingSuggestions=true)`` (#489) before it ends the
                # generator. ``subscribe`` owns the exactly-one-terminal +
                # bounded-grace lifecycle, so the loop ends when it stops yielding
                # (immediately for a non-pending terminal — unchanged behaviour).
                log.info("ws_chat.terminal", stream_id=stream_id, terminal=envelope.get("type"))
    except WebSocketDisconnect:
        # Client closed mid-stream — stop relaying (the producer is decoupled and
        # continues/finishes independently; cancellation of generation is handled
        # by the runtime task).
        log.info("ws_chat.disconnect", stream_id=stream_id)
    finally:
        await subscription.aclose()
