"""Chat sessions + messages use-cases (CC-6 #24 / CC-11 #26).

The orchestration layer for the ``/chat`` surface (ADR-0004: ``services/``
compose adapters; routers call exactly one service). It pairs the tenant-scoped
``db/`` chat repositories (the only SQL) with the #47 model allow-list and turns
the storage-faithful entities into the wire projections the contract requires
(``message_count`` computed here; citations hydrated for history).

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2 — deny by default).**
Every operation is scoped to the caller's tenant (the repository) *and* the
caller's ownership (this service). A session in another tenant — or owned by
another user — is treated as **non-existent**: the read/update returns ``None``
and the router maps that to **404** (existence non-disclosure; never 403). The
``owner_id``/``tenant_id`` come from the resolved principal, never request input.

**Send (the 202 contract).** :meth:`send_message` validates the (optional)
per-turn ``model`` against the allow-list (unknown → 422, INV-8), persists the
**user** message, and returns the persisted message + a fresh ``stream_id``. The
*answer* is produced asynchronously by the :class:`~app.services.chat_runtime`
runtime and streamed over the WS backplane keyed by that id — this service owns
only the synchronous, transactional part (the user turn); the router launches the
runtime after committing.

Cursor pagination mirrors the collections/documents keyset: an opaque cursor
encodes the boundary row **id**; the repository resolves its timestamp in-DB. A
malformed cursor is rejected fail-closed (422).
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ValidationError
from app.db.repositories import (
    ChatSessionRepository,
    CitationRepository,
    CitationView,
    MessageRepository,
    UserPreferenceRepository,
)
from app.domain.entities import ChatSession, Message, MessageRole
from app.realtime.backplane import Backplane, StreamOwner
from app.services.models_service import is_allowed_model

_MIN_LIMIT = 1
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20

# How many prior turns to include as conversation context for the answer (keeps
# the prompt bounded; the runtime adds the system prompt + the new question).
_HISTORY_TURNS = 20


@dataclass(frozen=True, slots=True)
class SessionView:
    """A chat session projected for the wire (contract ``ChatSession``)."""

    session: ChatSession
    message_count: int


@dataclass(frozen=True, slots=True)
class SessionPage:
    """One page of sessions + the opaque cursor for the next page."""

    items: list[SessionView]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class MessageView:
    """A message + its hydrated citations (contract ``Message``)."""

    message: Message
    citations: list[CitationView]


@dataclass(frozen=True, slots=True)
class MessagePage:
    """One page of messages (oldest → newest) + the next cursor."""

    items: list[MessageView]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class SendResult:
    """The outcome of a send: the persisted user message + the answer stream id.

    The router serialises ``user_message`` into the 202 ``SendMessageResponse``
    and uses ``stream_id`` / ``model`` / ``history`` to launch the answer runtime.
    """

    user_message: Message
    stream_id: str
    model: str
    history: tuple[Message, ...]


# --- Cursor codec (opaque; carries the boundary row id) ---------------------

_SESSION_CURSOR_PREFIX = "chs:"
_MESSAGE_CURSOR_PREFIX = "msg:"


def _encode_cursor(prefix: str, row_id: UUID) -> str:
    raw = f"{prefix}{row_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(prefix: str, cursor: str) -> UUID:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc
    if not raw.startswith(prefix):
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor")
    try:
        return UUID(raw[len(prefix) :])
    except ValueError as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return _DEFAULT_LIMIT
    return max(_MIN_LIMIT, min(_MAX_LIMIT, limit))


class ChatService:
    """Create / list / get / update / delete sessions + send/list messages.

    Constructed per-request with the session, the resolved ``tenant_id`` /
    ``owner_id`` (both from the token), and the process ``Settings`` (the model
    allow-list + default model). All ownership/tenancy enforcement lives here.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        settings: Settings,
    ) -> None:
        self._sessions = ChatSessionRepository(session, tenant_id)
        self._messages = MessageRepository(session, tenant_id)
        self._citations = CitationRepository(session, tenant_id)
        self._prefs = UserPreferenceRepository(session, tenant_id)
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._settings = settings

    # --- model selection ----------------------------------------------------

    def _default_model(self) -> str:
        for m in self._settings.chat_model_registry:
            if m.is_default:
                return m.id
        # The settings validator guarantees exactly one default; this is belt-and
        # braces for the type-checker.
        return self._settings.chat_model_registry[0].id

    def _resolve_model(self, requested: str | None) -> str:
        """Validate the requested model against the allow-list, or use the default.

        ``None`` ⇒ the configured server default. A non-empty unknown id is
        rejected (422, INV-8) before any session is created or message persisted.
        """
        if requested is None:
            return self._default_model()
        if not is_allowed_model(requested, self._settings):
            raise ValidationError(f"Unknown model {requested!r}.", code="unknown_model")
        return requested

    async def _resolved_default_model(self) -> str:
        """The caller's effective default for a NEW session (spec 0005 AC-P4).

        Their stored preference default if set *and* still in the registry;
        otherwise the server default. Fail-closed: a stored model that has since
        left the registry is ignored rather than erroring, so a removed model
        never strands a new chat.
        """
        prefs = await self._prefs.get(self._owner_id)
        if (
            prefs is not None
            and prefs.default_model is not None
            and is_allowed_model(prefs.default_model, self._settings)
        ):
            return prefs.default_model
        return self._default_model()

    # --- ownership ----------------------------------------------------------

    def _owns(self, session: ChatSession) -> bool:
        return session.owner_id == self._owner_id

    async def _view(self, session: ChatSession) -> SessionView:
        count = await self._sessions.count_messages(session.id)
        return SessionView(session=session, message_count=count)

    # --- session use-cases --------------------------------------------------

    async def create_session(self, *, title: str | None, model: str | None) -> SessionView:
        """Create a session owned by the caller.

        With no explicit ``model`` the caller's saved **default-model preference**
        seeds it (spec 0005 AC-P4), falling back to the server default; an explicit
        model is validated against the allow-list (unknown → 422, INV-8).
        """
        resolved = (
            await self._resolved_default_model() if model is None else self._resolve_model(model)
        )
        session = await self._sessions.create(
            owner_id=self._owner_id, model=resolved, title=title or ""
        )
        return SessionView(session=session, message_count=0)

    async def list_sessions(self, *, cursor: str | None, limit: int | None) -> SessionPage:
        """A keyset page of the caller's own sessions (newest-updated first)."""
        page_size = _clamp_limit(limit)
        after_id = _decode_cursor(_SESSION_CURSOR_PREFIX, cursor) if cursor else None
        rows = await self._sessions.list_for_owner_page(
            self._owner_id, limit=page_size + 1, after_id=after_id
        )
        has_more = len(rows) > page_size
        page = rows[:page_size]
        next_cursor = (
            _encode_cursor(_SESSION_CURSOR_PREFIX, page[-1].id) if has_more and page else None
        )
        items = [await self._view(s) for s in page]
        return SessionPage(items=items, next_cursor=next_cursor)

    async def get_session(self, session_id: UUID) -> SessionView | None:
        """Fetch one of the caller's sessions, or ``None`` if not visible (→ 404)."""
        session = await self._sessions.get(session_id)
        if session is None or not self._owns(session):
            return None
        return await self._view(session)

    async def update_session(
        self, session_id: UUID, *, title: str | None, model: str | None
    ) -> SessionView | None:
        """Rename / change the default model of one of the caller's sessions.

        Visibility (tenant + ownership) is established before any write so a
        non-owner's session is never mutated and is reported as 404 (INV-2). A
        supplied ``model`` is validated against the allow-list (422 if unknown).
        """
        existing = await self._sessions.get(session_id)
        if existing is None or not self._owns(existing):
            return None
        resolved_model = self._resolve_model(model) if model is not None else None
        updated = await self._sessions.update(session_id, title=title, model=resolved_model)
        if updated is None:  # pragma: no cover — visibility already established
            return None
        return await self._view(updated)

    async def delete_session(self, session_id: UUID) -> bool:
        """Delete one of the caller's sessions (cascades to messages/citations)."""
        existing = await self._sessions.get(session_id)
        if existing is None or not self._owns(existing):
            return False
        return await self._sessions.delete(session_id)

    # --- message use-cases --------------------------------------------------

    async def list_messages(
        self, session_id: UUID, *, cursor: str | None, limit: int | None
    ) -> MessagePage | None:
        """A keyset page of a session's messages (oldest → newest) with citations.

        Returns ``None`` if the session is not visible to the caller (→ 404).
        Citations are hydrated in one batched join (no N+1); assistant messages
        carry their passage-level citations, user messages carry ``[]``.
        """
        session = await self._sessions.get(session_id)
        if session is None or not self._owns(session):
            return None
        page_size = _clamp_limit(limit)
        after_id = _decode_cursor(_MESSAGE_CURSOR_PREFIX, cursor) if cursor else None
        rows = await self._messages.list_for_session_page(
            session_id, limit=page_size + 1, after_id=after_id
        )
        has_more = len(rows) > page_size
        page = rows[:page_size]
        next_cursor = (
            _encode_cursor(_MESSAGE_CURSOR_PREFIX, page[-1].id) if has_more and page else None
        )
        citations_by_message = await self._citations.list_for_messages_hydrated(
            [m.id for m in page]
        )
        items = [MessageView(message=m, citations=citations_by_message.get(m.id, [])) for m in page]
        return MessagePage(items=items, next_cursor=next_cursor)

    async def send_message(
        self, session_id: UUID, *, content: str, model: str | None, backplane: Backplane
    ) -> SendResult | None:
        """Persist a user message and prepare the answer stream (the 202 path).

        Returns ``None`` if the session is not visible to the caller (→ 404). The
        per-turn ``model`` (or the session default) is validated against the
        allow-list (422 if unknown) *before* anything is persisted. On success the
        user message is added, the session is touched (re-sorts the list), and a
        fresh ``stream_id`` + the recent history are returned for the runtime. The
        caller commits, then launches the runtime.

        The minted ``stream_id`` is **bound to the asking principal** (owner +
        tenant) on the backplane before it is handed back (INV-1/INV-2): a bare
        random id carries no identity, so without this binding any authenticated
        — even cross-tenant — client that learned the id could subscribe and read
        another user's answer (incl. permitted-only citation snippets). The WS
        consumer verifies this binding before relaying a single envelope.
        """
        session = await self._sessions.get(session_id)
        if session is None or not self._owns(session):
            return None
        # Per-turn override validated against the allow-list; else the session's
        # default (itself a validated allow-list id at create/update time).
        resolved_model = self._resolve_model(model) if model is not None else session.model
        prior = await self._messages.list_for_session(session_id)
        user_message = await self._messages.add(
            session_id=session_id,
            role=MessageRole.USER,
            content=content,
            model=None,
        )
        await self._sessions.touch(session_id)
        stream_id = uuid.uuid4().hex
        await backplane.bind_owner(
            stream_id,
            StreamOwner(owner_id=self._owner_id, tenant_id=self._tenant_id),
        )
        return SendResult(
            user_message=user_message,
            stream_id=stream_id,
            model=resolved_model,
            history=tuple(prior[-_HISTORY_TURNS:]),
        )


__all__ = [
    "ChatService",
    "MessagePage",
    "MessageView",
    "SendResult",
    "SessionPage",
    "SessionView",
]
