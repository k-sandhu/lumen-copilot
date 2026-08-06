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
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import DependencyError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.repositories import (
    AssistantRepository,
    AssistantVersionRepository,
    ChatSessionRepository,
    CitationRepository,
    CitationView,
    GroupRepository,
    LlmProviderRepository,
    LlmUsageRepository,
    MessageRepository,
    SessionSummaryRepository,
    ToolInvocationRepository,
    UserPreferenceRepository,
)
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import (
    AssistantStatus,
    AuditOutcome,
    ChatSession,
    LlmUsageRecord,
    LlmUsageTotals,
    Message,
    MessageRole,
    ToolInvocation,
)
from app.llm.context import ContextConfig, input_budget_for_model
from app.realtime.backplane import Backplane, StreamOwner
from app.retrieval.permissions import AllowSet
from app.retrieval.queries import permitted_document_ids
from app.services.assistant_runtime import AssistantRunConfig, assemble_run_config
from app.services.audit import AuditSink
from app.services.models_service import is_allowed_model
from app.services.provider_models import (
    is_allowed_provider_model,
    is_provider_model_id,
    parse_provider_model_id,
)

_MIN_LIMIT = 1
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20

log = get_logger(__name__)

# History is no longer pre-sliced to a fixed count here (#424 re-review): the
# already-loaded prior messages are handed WHOLE to the runtime's token-budgeted
# context assembler (ADR-0016 §1 / #410), which keeps as many recent turns as the
# model's input window allows and trims the rest oldest-first, *logging* what it
# drops. A fixed slice here would silently discard turns that would have fit.
# (The unbounded ``list_for_session`` load is pre-existing; token-budget-driven
# reverse paging of that query is a separate optimisation — issue #428.)


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
    """A message + its hydrated citations and tool trace (contract ``Message``).

    ``tool_invocations`` (#377) carries the governed tool calls behind an
    assistant message (oldest first; ``[]`` for user messages) so a settled
    answer's tool activity stays visible in the thread, not only in the audit log.
    """

    message: Message
    citations: list[CitationView]
    tool_invocations: list[ToolInvocation] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MessagePage:
    """One page of messages (oldest → newest) + the next cursor."""

    items: list[MessageView]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class SessionUsageView:
    """A session's token/context accounting for the wire (spec 0007 #429).

    ``totals`` sums every produced answer's usage; ``last`` is the most recent
    answer's record (``None`` before the first answer); ``input_budget_tokens``
    is the assembler's input budget for the session's model (window − output
    headroom − safety margin) with ``window_known=False`` when the model was
    absent from the model map and the configured fallback window was used.
    """

    model: str
    totals: LlmUsageTotals
    last: LlmUsageRecord | None
    input_budget_tokens: int
    window_known: bool


@dataclass(frozen=True, slots=True)
class SendResult:
    """The outcome of a send: the persisted user message + the answer stream id.

    The router serialises ``user_message`` into the 202 ``SendMessageResponse``
    and uses ``stream_id`` / ``model`` / ``history`` to launch the answer runtime.
    ``assistant_config`` is the assembled run config when the session is pinned to
    an assistant version (ADR-0011 §2), else ``None`` (ad-hoc chat).
    ``custom_instructions`` is the asking user's per-account preamble (from their
    preferences, resolved here where the principal + session are already held); the
    runtime prepends it to the system prompt before the grounding contract.
    """

    user_message: Message
    stream_id: str
    model: str
    history: tuple[Message, ...]
    assistant_config: AssistantRunConfig | None = None
    custom_instructions: str | None = None
    #: The session's rolling summary text (#416) — ``None`` until the async
    #: summarizer has produced one. ``history`` then contains only turns NEWER
    #: than the summary's coverage.
    summary: str | None = None
    #: The previous answer's cited-evidence digest — ID pairs only; the runtime
    #: rehydrates them under the requester's CURRENT permissions (INV-2).
    evidence: tuple[tuple[UUID, UUID], ...] = ()
    #: {document_id: name} the summary text mentions — the runtime redacts the
    #: names of no-longer-permitted documents before the text reaches a prompt.
    mentioned_documents: tuple[tuple[UUID, str], ...] = ()


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


class SandboxLifecycle(Protocol):
    """Narrow deletion hook; the sandbox module owns container lifecycle."""

    async def close(self, chat_session_id: UUID) -> None: ...


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
        sandbox_lifecycle: SandboxLifecycle | None = None,
        audit: AuditSink | None = None,
        request_id: str = "unknown",
        source_ip: str = "unknown",
    ) -> None:
        self._sessions = ChatSessionRepository(session, tenant_id)
        self._messages = MessageRepository(session, tenant_id)
        self._citations = CitationRepository(session, tenant_id)
        self._tool_invocations = ToolInvocationRepository(session, tenant_id)
        self._prefs = UserPreferenceRepository(session, tenant_id)
        self._summaries = SessionSummaryRepository(session, tenant_id)
        self._assistants = AssistantRepository(session, tenant_id)
        self._versions = AssistantVersionRepository(session, tenant_id)
        # The tenant's LLM providers — the DB half of the model allow-list: a
        # ``provider:`` model id is valid only if it resolves to an enabled provider
        # in this tenant with the raw id in its discovered snapshot (PR 2a, INV-1).
        self._providers = LlmProviderRepository(session, tenant_id)
        self._usage = LlmUsageRepository(session, tenant_id)
        self._groups = GroupRepository(session, tenant_id)
        self._session = session
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._settings = settings
        self._sandbox_lifecycle = sandbox_lifecycle
        # Optional so the many tests that construct this service directly need no
        # change; a transcript read that redacts nothing emits nothing either way.
        self._audit = audit
        self._request_id = request_id
        self._source_ip = source_ip
        self._allow_set_cache: AllowSet | None = None

    # --- model selection ----------------------------------------------------

    def _default_model(self) -> str:
        for m in self._settings.chat_model_registry:
            if m.is_default:
                return m.id
        # The settings validator guarantees exactly one default; this is belt-and
        # braces for the type-checker.
        return self._settings.chat_model_registry[0].id

    async def _is_allowed(self, model_id: str) -> bool:
        """Whether ``model_id`` is a config OR a valid tenant provider model (PR 2a).

        A ``provider:`` id is checked against the tenant's enabled providers + their
        discovered snapshot (unknown / disabled / cross-tenant → False); any other
        id falls back to the pure config allow-list — so a built-in model still
        validates without a DB hit.
        """
        if is_provider_model_id(model_id):
            return await is_allowed_provider_model(model_id, self._providers)
        return is_allowed_model(model_id, self._settings)

    async def _resolve_model(self, requested: str | None) -> str:
        """Validate the requested model against the allow-list, or use the default.

        ``None`` ⇒ the configured server default. A non-empty unknown id is
        rejected (422, INV-8) before any session is created or message persisted.
        A ``provider:`` id is validated against the tenant's enabled providers
        (PR 2a) so a discovered provider model is selectable.
        """
        if requested is None:
            return self._default_model()
        if not await self._is_allowed(requested):
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
            and await self._is_allowed(prefs.default_model)
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

    async def create_session(
        self, *, title: str | None, model: str | None, assistant_id: UUID | None = None
    ) -> SessionView:
        """Create a session owned by the caller.

        With no explicit ``model`` the caller's saved **default-model preference**
        seeds it (spec 0005 AC-P4), falling back to the server default; an explicit
        model is validated against the allow-list (unknown → 422, INV-8).

        With an ``assistant_id`` (ADR-0011 §5) the server resolves the assistant
        (cross-tenant / non-owned / non-granted → 404, existence non-disclosure),
        **pins its current published version** (a non-published / disabled
        assistant → 422), and stamps both FKs on the session. The assistant's
        model default seeds the session model when the caller supplied none. Ad-hoc
        (no ``assistant_id``) is unchanged.
        """
        if assistant_id is None:
            resolved = (
                await self._resolved_default_model()
                if model is None
                else await self._resolve_model(model)
            )
            session = await self._sessions.create(
                owner_id=self._owner_id, model=resolved, title=title or ""
            )
            return SessionView(session=session, message_count=0)

        assistant = await self._assistants.get(assistant_id)
        if assistant is None or assistant.owner_id != self._owner_id:
            # Cross-tenant / non-owned / non-granted → 404 (INV-1/INV-2). Sharing
            # via grants is a follow-up (ADR-0011 §1); for now only the owner may
            # start a session from an assistant.
            raise NotFoundError("Assistant not found.")
        if assistant.status is not AssistantStatus.PUBLISHED:
            raise ValidationError(
                "Only a published assistant can start a session.",
                code="assistant_not_published",
            )
        head = await self._versions.get_head(assistant.id)
        if head is None:  # pragma: no cover — published implies a version exists
            raise ValidationError(
                "The assistant has no published version.", code="assistant_not_published"
            )
        # Model precedence: an explicit request model wins; else the assistant's
        # frozen model default; else the caller's / server default (fail-closed).
        if model is not None:
            resolved = await self._resolve_model(model)
        else:
            frozen_model = head.config.get("model")
            resolved = (
                frozen_model
                if isinstance(frozen_model, str)
                and frozen_model
                and await self._is_allowed(frozen_model)
                else await self._resolved_default_model()
            )
        session = await self._sessions.create(
            owner_id=self._owner_id,
            model=resolved,
            title=title or "",
            assistant_id=assistant.id,
            assistant_version_id=head.id,
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
        # One grouped count for the whole page (no per-row COUNT — #396); the
        # single-session paths keep `_view`.
        counts = await self._sessions.count_for_sessions([s.id for s in page])
        items = [SessionView(session=s, message_count=counts.get(s.id, 0)) for s in page]
        return SessionPage(items=items, next_cursor=next_cursor)

    async def get_session(self, session_id: UUID) -> SessionView | None:
        """Fetch one of the caller's sessions, or ``None`` if not visible (→ 404)."""
        session = await self._sessions.get(session_id)
        if session is None or not self._owns(session):
            return None
        return await self._view(session)

    async def session_usage(self, session_id: UUID) -> SessionUsageView | None:
        """The session's token/context accounting (spec 0007 #429), or ``None`` (→ 404).

        Sums the session's ``llm_usage`` rows (#409) and reports the input-token
        budget the assembler would grant the session's model — the SAME formula
        answers are actually assembled under (``input_budget_for_model``), so the
        meter can show "last prompt vs window" without a parallel approximation.
        Visibility first (tenant + ownership → 404, INV-1/INV-2); a session with
        no answers yields all-zero totals, never an error.
        """
        session = await self._sessions.get(session_id)
        if session is None or not self._owns(session):
            return None
        totals = await self._usage.totals_for_session(session_id)
        last = await self._usage.last_for_session(session_id)
        # The budget follows the model that ACTUALLY produced the last answer
        # (#434 NEW-2): a per-turn model override records its id on the usage
        # row, so the meter must budget against that — the session default only
        # covers a fresh session with no answers yet. And it resolves a
        # per-tenant ``provider:`` id to its RAW model id before the window
        # lookup (#434 finding 3) — exactly like the runtime's route — so a
        # known-window provider model never falls back to the conservative
        # window. The response's ``model`` is the id the budget was computed
        # for, in its public form.
        source_model = last.model if last is not None else session.model
        parsed = parse_provider_model_id(source_model)
        budget_model = parsed[1] if parsed is not None else source_model
        budget, window_known = input_budget_for_model(
            budget_model,
            ContextConfig(
                fallback_max_input_tokens=self._settings.context_fallback_max_input_tokens,
                output_headroom_tokens=self._settings.context_output_headroom_tokens,
            ),
        )
        return SessionUsageView(
            model=source_model,
            totals=totals,
            last=last,
            input_budget_tokens=budget,
            window_known=window_known,
        )

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
        resolved_model = await self._resolve_model(model) if model is not None else None
        updated = await self._sessions.update(session_id, title=title, model=resolved_model)
        if updated is None:  # pragma: no cover — visibility already established
            return None
        return await self._view(updated)

    async def delete_session(self, session_id: UUID) -> bool:
        """Delete one of the caller's sessions (cascades to messages/citations)."""
        existing = await self._sessions.get(session_id)
        if existing is None or not self._owns(existing):
            return False
        if self._sandbox_lifecycle is not None:
            try:
                await self._sandbox_lifecycle.close(session_id)
            except DependencyError as exc:
                # Deleting user data must not depend on runner availability. The DB
                # cascade removes the lifecycle row; the labelled container is safe to
                # reap later because it has no host mounts, socket, secrets, or network.
                log.warning(
                    "chat.sandbox_cleanup_deferred",
                    chat_session_id=str(session_id),
                    error_type=type(exc).__name__,
                )
        return await self._sessions.delete(session_id)

    # --- message use-cases --------------------------------------------------

    async def _resolve_allow_set(self) -> AllowSet:
        """The requester's read allow-set, including their group principals.

        Mirrors ``DocumentService._resolve_allow_set``: membership is resolved per
        request and never cached across them, so removing someone from a group
        revokes on the next read. A user in no groups narrows to ownership plus
        their own user grants — fail closed.
        """
        if self._allow_set_cache is None:
            group_ids = await self._groups.group_ids_for_user(self._owner_id)
            self._allow_set_cache = AllowSet.for_user(
                tenant_id=self._tenant_id, user_id=self._owner_id, group_ids=group_ids
            )
        return self._allow_set_cache

    async def _filter_citations(
        self,
        citations_by_message: dict[uuid.UUID, list[CitationView]],
        *,
        session_id: UUID,
    ) -> dict[uuid.UUID, list[CitationView]]:
        """Re-check every hydrated citation against CURRENT permissions (#536, INV-2).

        The hydration join is tenant-scoped only. A citation is written only for a
        passage the asker could retrieve, but that is a fact about **write** time:
        revoke the grant afterwards and the row — including the full ``chunks.text``
        — keeps coming back on every transcript read. The answer path already
        re-checks (evidence carry-forward stores ids and rehydrates under current
        permissions) and the summariser redacts cited spans out of summary prose;
        the transcript was the one surface still serving the passage itself.

        Redacted rather than dropped: removing the citation outright would erase a
        settled claim's provenance, which is its own kind of dishonesty. The shell
        stays so the UI can say "source no longer available".

        One extra query per page, over the page's distinct document ids.
        """
        if not citations_by_message:
            return citations_by_message
        document_ids = {c.document_id for cites in citations_by_message.values() for c in cites}
        if not document_ids:
            return citations_by_message
        permitted = await permitted_document_ids(
            self._session,
            allow_set=await self._resolve_allow_set(),
            document_ids=sorted(document_ids),
        )
        redacted_count = 0
        filtered: dict[uuid.UUID, list[CitationView]] = {}
        for message_id, cites in citations_by_message.items():
            out = []
            for c in cites:
                if c.document_id in permitted:
                    out.append(c)
                else:
                    out.append(c.redact())
                    redacted_count += 1
            filtered[message_id] = out
        if redacted_count and self._audit is not None:
            # INV-6: a read that withheld content is a permission-relevant event.
            # Counts only — never which passage, and never its text.
            await self._audit.emit(
                action=AuditAction.EVIDENCE_REHYDRATED,
                actor=AuditActor.user(self._owner_id),
                resource_type="session",
                resource_id=str(session_id),
                outcome=AuditOutcome.ALLOWED,
                request_id=self._request_id,
                source_ip=self._source_ip,
                metadata={
                    "surface": "transcript",
                    "requested_documents": len(document_ids),
                    "permitted_documents": len(permitted),
                    "redacted_citations": redacted_count,
                },
            )
        return filtered

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
        # INV-2 on the READ, not only at write time (#536).
        citations_by_message = await self._filter_citations(
            citations_by_message, session_id=session_id
        )
        # The governed tool trace per assistant message (#377) — batched like
        # citations (no N+1); user messages simply have no rows.
        tools_by_message = await self._tool_invocations.list_for_messages([m.id for m in page])
        items = [
            MessageView(
                message=m,
                citations=citations_by_message.get(m.id, []),
                tool_invocations=tools_by_message.get(m.id, []),
            )
            for m in page
        ]
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
        resolved_model = await self._resolve_model(model) if model is not None else session.model
        # If the session is pinned to an assistant version, assemble its run config
        # (ADR-0011 §2): instructions → system prompt, tool_allowlist → allowed set,
        # knowledge_scope → the narrowing retrieval filter. Load the exact pinned
        # version so the run is reproducible even after the assistant is edited.
        assistant_config = await self._load_assistant_config(session.assistant_version_id)
        # The asking user's per-account custom instructions (spec: user settings). Read
        # from THEIR preferences row (tenant-scoped, keyed off the resolved owner — never
        # request input); threaded to the runtime, which prepends it to the system prompt
        # before the grounding contract. A fresh user / cleared value is ``None`` (ad-hoc
        # chat, unchanged). Resolved here where the principal + session are already held,
        # not inside the gateway.
        custom_instructions = await self._resolve_custom_instructions()
        # Rolling-summary window (#416, ADR-0016 §3.2): turns the summary
        # already covers are NOT resent verbatim — the runtime gets
        # [summary] + [newer turns], filtered IN SQL by the durable coverage
        # cursor ((created_at, id), #446 finding 5 — valid even after the
        # boundary row is pruned, and never a full-conversation Python scan).
        # No summary/cursor ⇒ the full prior list — today's behavior (AC-4).
        summary_row = await self._summaries.get_for_session(session_id)
        summary_text: str | None = None
        evidence: tuple[tuple[UUID, UUID], ...] = ()
        mentioned: tuple[tuple[UUID, str], ...] = ()
        if summary_row is not None:
            summary_text = summary_row.summary
            evidence = summary_row.evidence
            mentioned = summary_row.mentioned_documents
        if (
            summary_row is not None
            and summary_text
            and summary_row.covers_through_created_at is not None
            and summary_row.covers_through_message_id is not None
        ):
            prior = await self._messages.list_for_session_after(
                session_id,
                after_created_at=summary_row.covers_through_created_at,
                after_message_id=summary_row.covers_through_message_id,
            )
        else:
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
            history=tuple(prior),
            assistant_config=assistant_config,
            custom_instructions=custom_instructions,
            summary=summary_text,
            evidence=evidence,
            mentioned_documents=mentioned,
        )

    async def _resolve_custom_instructions(self) -> str | None:
        """The asking user's stored custom instructions for this turn, or ``None``.

        Reads the caller's own preferences row (tenant-scoped repository, keyed off
        the resolved ``owner_id``). A fresh user (no row) or a cleared value yields
        ``None`` — the runtime then composes the system prompt exactly as ad-hoc chat.
        """
        prefs = await self._prefs.get(self._owner_id)
        if prefs is None:
            return None
        return prefs.custom_instructions

    async def _load_assistant_config(
        self, assistant_version_id: UUID | None
    ) -> AssistantRunConfig | None:
        """Assemble the run config for a session's pinned assistant version (or None).

        Tenant-scoped (the repository); a version pinned in this tenant is loaded
        and assembled into the three run inputs. A missing/foreign version id
        (e.g. the assistant was deleted, nulling the FK) degrades to ``None`` — the
        turn runs as ad-hoc rather than failing, fail-open on availability but never
        on permission (the per-user retrieval filter still runs).
        """
        if assistant_version_id is None:
            return None
        version = await self._versions.get(assistant_version_id)
        if version is None:
            return None
        return assemble_run_config(version.config)


__all__ = [
    "ChatService",
    "MessagePage",
    "MessageView",
    "SendResult",
    "SessionPage",
    "SessionView",
]
