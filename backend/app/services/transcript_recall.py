"""The own-conversation read seam behind ``read_conversation`` (#569, epic #533 R1).

The half of recall that reaches the database. It owns the three properties a tool
handler must not be trusted with, because a handler runs on arguments the *model*
chose:

1. **Ownership.** RLS on ``chat_sessions``/``messages`` is **tenant-only**
   (``20260624_0000-0007_tenancy_rls.py`` — the policy is keyed on ``tenant_id``
   alone). A cross-tenant read is stopped by the database; a same-tenant,
   wrong-owner read is stopped by exactly one app-layer predicate,
   ``ChatService._owns`` — and a tool handler does not route through
   ``ChatService``. So this seam re-checks ownership itself instead of trusting
   that its constructor was called from the right place.
2. **The compacted-range bound.** Recall reaches only turns the summariser has
   already folded out of the prompt. Anything still in the live window is
   verbatim in the model's context already, so returning it would spend budget
   restating what the budget already holds.
3. **The stopping rule.** A per-answer call budget and a repeat-query
   short-circuit, enforced here rather than described in the tool's prompt text.
   A limit the model is *asked* to respect is a suggestion; a limit inside the
   only object that can reach ``messages`` is a limit.

It hands back **ids, never prose provenance**: the caller re-checks every cited
document against current permissions before a single recalled character reaches
the model (epic #533 design rule 1).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.core.logging import get_logger
from app.db.repositories import (
    ChatSessionRepository,
    CitationRepository,
    MessageRepository,
    SessionSummaryRepository,
)
from app.services.tools.types import RecalledTurn, RecallOutcome

logger = get_logger(__name__)

#: Recall calls allowed in ONE answer. Two is enough for the real pattern — look,
#: then look again with better keywords — and stops the failure mode where a
#: model that cannot find what it half-remembers keeps rephrasing instead of
#: asking the user.
_MAX_CALLS_PER_ANSWER = 2

#: Mirrors the tool's own bounds so the seam is safe when called directly (a
#: sub-agent, a future caller) and not only behind today's handler.
_MAX_TERMS = 6
_MAX_TERM_CHARS = 64

_BUDGET_SPENT = (
    "You have already read back this conversation as many times as this answer "
    "allows. Answer with what you have, or ask the user to restate the part you "
    "are missing — do not call read_conversation again."
)
_ALREADY_ASKED = (
    "You already ran that exact search this turn and saw its result above. "
    "Either use it, search for something different, or ask the user."
)


class _RecallBudget:
    """The per-ANSWER stopping rule, held apart from any one DB session.

    Separate from the reader because a fanned-out call gets its own
    ``AsyncSession`` (#412) and therefore its own reader *view* — but must not
    get its own allowance. Issuing two recalls in the same concurrent batch is
    the obvious way to buy extra calls, and it would work if the counter lived on
    the view.

    Safe without a lock under asyncio: :meth:`claim` has no ``await`` between the
    check and the mutation, so it cannot interleave.
    """

    def __init__(self, max_calls: int) -> None:
        self._max_calls = max_calls
        self._calls = 0
        self._asked: set[str] = set()

    def claim(self, key: str) -> str | None:
        """Take one call for ``key``; return the refusal sentence if refused."""
        if key in self._asked:
            # Checked BEFORE the budget so a repeat never burns a call the model
            # could have spent on a better query.
            return _ALREADY_ASKED
        if self._calls >= self._max_calls:
            return _BUDGET_SPENT
        self._calls += 1
        self._asked.add(key)
        return None


class SessionTranscriptReader:
    """A :class:`~app.services.tools.types.TranscriptReader` bound to one session.

    Constructed per answer by the chat runtime, so its budget is naturally
    per-answer: the budget object dies with the turn. Holds an ``AsyncSession``
    — the runtime's on the serial path, an isolated call-scope session on the
    concurrent one (see :meth:`for_session`). Every read below is a read, so it
    neither commits nor dirties the transaction it borrows.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        principal: Principal,
        session_id: UUID,
        max_calls: int = _MAX_CALLS_PER_ANSWER,
        budget: _RecallBudget | None = None,
    ) -> None:
        self._principal = principal
        self._session_id = session_id
        self._budget = budget if budget is not None else _RecallBudget(max_calls)
        self._sessions = ChatSessionRepository(session, principal.tenant_id)
        self._messages = MessageRepository(session, principal.tenant_id)
        self._summaries = SessionSummaryRepository(session, principal.tenant_id)
        self._citations = CitationRepository(session, principal.tenant_id)

    def for_session(self, session: AsyncSession) -> SessionTranscriptReader:
        """A view on the same conversation over ``session``, sharing the budget.

        The concurrent executor (#412) hands each fanned-out read-only call its
        own ``AsyncSession``; a seam that kept the runtime's would be used from
        two coroutines at once — the same hazard that keeps MCP tools out of the
        fan-out entirely. The budget is passed by reference, deliberately: the
        session is per-call, the allowance is per-answer.
        """
        return SessionTranscriptReader(
            session=session,
            principal=self._principal,
            session_id=self._session_id,
            budget=self._budget,
        )

    async def recall(self, *, query: str | None, limit: int) -> RecallOutcome:
        """Read the compacted range of the bound session; ids only for evidence."""
        refusal = self._budget.claim(" ".join((query or "").lower().split()))
        if refusal is not None:
            return RecallOutcome(refusal=refusal)

        chat_session = await self._sessions.get(self._session_id)
        if chat_session is None or chat_session.owner_id != self._principal.user_id:
            # The runtime only builds this seam for the session it is answering,
            # so reaching here means a miswiring, not an attack. Report nothing
            # (INV-2 existence non-disclosure — an owner-shaped error would tell
            # the caller the session exists), but LOG it: a redaction that is
            # also invisible is a bug nobody finds.
            logger.warning(
                "transcript_recall.owner_mismatch",
                extra={"session_id": str(self._session_id)},
            )
            return RecallOutcome()

        summary = await self._summaries.get_for_session(self._session_id)
        # The compaction predicate must stay in LOCKSTEP with
        # ``ChatService.send_message``: it sends the full history unless the row
        # has BOTH a non-empty summary and both cursor halves. A row with a
        # cursor but no text compacts nothing, so recall must find nothing —
        # otherwise the model reads turns it can already see.
        if (
            summary is None
            or not summary.summary
            or summary.covers_through_created_at is None
            or summary.covers_through_message_id is None
        ):
            return RecallOutcome(compaction_started=False)

        terms = [t[:_MAX_TERM_CHARS] for t in (query or "").split()][:_MAX_TERMS]
        rows = await self._messages.search_for_session_before(
            self._session_id,
            before_created_at=summary.covers_through_created_at,
            before_message_id=summary.covers_through_message_id,
            terms=terms,
            limit=max(1, limit),
        )
        if not rows:
            return RecallOutcome()

        cited = await self._citations.document_ids_for_messages([row.id for row in rows])
        turns = tuple(
            RecalledTurn(
                role=row.role.value,
                created_at=row.created_at,
                content=row.content,
                cited_document_ids=tuple(sorted(cited.get(row.id, set()), key=str)),
            )
            # The query returns newest-first (the most recent mention is what
            # "as we discussed earlier" means); reading order is oldest-first.
            for row in reversed(rows)
        )
        return RecallOutcome(turns=turns)


__all__ = ["SessionTranscriptReader"]
