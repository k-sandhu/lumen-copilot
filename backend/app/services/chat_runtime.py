"""The agentic, grounded RAG answer runtime (CC-6 #24 / CC-11 #26).

This is the **answer path**: given a persisted user message, it gives the
selected model the #45 retrieval tools, lets the model decide when to search,
grounds the answer in the permitted passages those searches return, streams the
answer + tool/citation events over the WS backplane, and persists the assistant
message with its citations (INV-3) and the audit trail (INV-6).

Layering (ADR-0004): orchestration only. It composes — never *is* — its
collaborators:

* the #36 ``llm/`` gateway (``stream_tools``) — the only model caller;
* the #45 ``retrieval/`` service tools (``search_text`` / ``search_documents`` /
  ``get_document``) — the only retrieval path, permission-filtered inside (INV-2);
* the ``realtime/`` backplane — the only pub/sub; the producer here publishes
  envelopes a decoupled WS consumer relays;
* the ``db/`` message + citation repositories (the only SQL) and the #23 audit
  sink (the only audit path).

**Grounding / INV-3 is structural, not prompt-only.** Citations are built *only*
from passages the retrieval tools actually returned (``GroundedCitation`` is
constructed from a ``RetrievedPassage``), so a citation can never reference a
passage the user could not retrieve, and the model cannot conjure one. A turn
that retrieved nothing yields a zero-citation answer, shown honestly as such —
the runtime prefers "I couldn't find it" over a confident, uncited answer.

**Lifecycle.** It publishes exactly the contract envelope sequence with a
monotonic ``seq``: ``start`` → ( ``delta`` | ``event:tool_call`` |
``event:tool_result`` | ``event:citation`` )* → exactly one terminal ``done`` |
``error``. Cancellation (client gone / shutdown) and any error both end the
stream with one terminal envelope and never leak a vendor error.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.principal import Principal
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.repositories import (
    AuditEventRepository,
    CitationRepository,
    MessageRepository,
)
from app.domain.audit import AuditAction, AuditActor
from app.domain.chat import GroundedCitation
from app.domain.entities import AuditOutcome, MessageRole
from app.domain.llm import ChatMessage, Role, ToolCall
from app.llm import LLMGateway
from app.realtime import envelopes
from app.realtime.backplane import Backplane
from app.retrieval import RetrievalService
from app.services.audit import AuditSink
from app.services.chat_tools import (
    TOOL_SPECS,
    ToolOutcome,
    run_tool,
)
from app.services.prompts import GROUNDED_SYSTEM_PROMPT, NO_SOURCES_FALLBACK

log = get_logger(__name__)

# How many tool-calling turns the agent may take before the runtime forces a
# final answer (bounds the loop — no unbounded multi-step planning, issue #24
# OUT). Each turn is one streamed completion that may request tools.
_MAX_TOOL_TURNS = 4
# Default passages per search (configurable budget; kept small for the MVP).
_DEFAULT_K = 6


@dataclass(slots=True)
class _StreamState:
    """Mutable per-stream bookkeeping (seq counter + accumulated answer)."""

    stream_id: str
    seq: int = 0

    def next_seq(self) -> int:
        s = self.seq
        self.seq += 1
        return s


class ChatRuntime:
    """Runs one grounded answer and streams it over the backplane.

    Constructed per answer with the collaborators it composes. The single entry
    point is :meth:`run`, intended to be launched as a background task off the
    202 send handler; it owns its own DB session (the request session is gone by
    then), commits the assistant turn, and publishes the terminal envelope.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        gateway: LLMGateway,
        backplane: Backplane,
        principal: Principal,
        request_id: str,
        source_ip: str,
        retrieval_factory: Callable[[AsyncSession], RetrievalService] | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._gateway = gateway
        self._backplane = backplane
        self._principal = principal
        self._request_id = request_id
        self._source_ip = source_ip
        # The retrieval service is built per-answer over the runtime's own
        # session. Injectable so the offline tests supply a fake whose
        # ``search_text`` does not need pgvector; defaults to the real adapter.
        self._retrieval_factory = retrieval_factory or (
            lambda session: RetrievalService(session, gateway=self._gateway)
        )

    async def run(
        self,
        *,
        stream_id: str,
        session_id: UUID,
        question: str,
        model: str,
        history: Sequence[ChatMessage],
        collection_ids: list[UUID] | None,
    ) -> None:
        """Produce the grounded answer for ``stream_id`` end-to-end.

        Publishes ``start``, runs the agentic tool loop (streaming ``delta`` and
        ``event`` envelopes), persists the assistant message + citations, then
        publishes exactly one terminal ``done``. Any error is mapped to a typed
        problem and published as the terminal ``error`` envelope (the vendor
        error never escapes). The assistant message id is minted up front so it
        rides ``start`` and is the row the citations attach to.
        """
        state = _StreamState(stream_id=stream_id)
        assistant_message_id = uuid.uuid4()
        await self._publish(
            state,
            envelopes.start(
                stream_id,
                state.next_seq(),
                data={
                    "sessionId": str(session_id),
                    "messageId": str(assistant_message_id),
                    "model": model,
                },
            ),
        )
        try:
            async with self._sessionmaker() as session:
                result = await self._answer(
                    session=session,
                    state=state,
                    session_id=session_id,
                    assistant_message_id=assistant_message_id,
                    question=question,
                    model=model,
                    history=history,
                    collection_ids=collection_ids,
                )
                await session.commit()
        except AppError as exc:
            await self._terminal_error(state, exc.status, exc.title, exc.code, exc.detail)
            return
        except Exception as exc:  # noqa: BLE001 — never leak a vendor error to the client
            # Log the error *type* only (never the message — it may carry vendor
            # details / a key). The client gets an opaque 500 problem envelope.
            log.error("chat_runtime.failed", stream_id=stream_id, error_type=type(exc).__name__)
            await self._terminal_error(state, 500, "Internal Server Error", "internal_error", None)
            return

        await self._publish(
            state,
            envelopes.done(
                stream_id,
                state.next_seq(),
                data={
                    "messageId": str(assistant_message_id),
                    "finishReason": result.finish_reason,
                    "citationCount": len(result.citations),
                    "usage": {
                        "promptTokens": result.prompt_tokens,
                        "completionTokens": result.completion_tokens,
                        "totalTokens": result.total_tokens,
                    },
                },
            ),
        )

    # --- the agentic loop ---------------------------------------------------

    async def _answer(
        self,
        *,
        session: AsyncSession,
        state: _StreamState,
        session_id: UUID,
        assistant_message_id: UUID,
        question: str,
        model: str,
        history: Sequence[ChatMessage],
        collection_ids: list[UUID] | None,
    ) -> _RunResult:
        """The tool-calling loop: search → ground → stream → persist."""
        tenant_id = self._principal.tenant_id
        retrieval = self._retrieval_factory(session)
        audit = AuditSink(AuditEventRepository(session, tenant_id))

        messages: list[ChatMessage] = [
            ChatMessage(role=Role.SYSTEM, content=GROUNDED_SYSTEM_PROMPT),
            *history,
            ChatMessage(role=Role.USER, content=question),
        ]

        # Citations keyed by chunk_id so the same passage cited across turns is
        # recorded once (INV-3 set), preserving first-seen order.
        cited: dict[UUID, GroundedCitation] = {}
        answer_parts: list[str] = []
        prompt_tokens = completion_tokens = total_tokens = 0
        finish_reason = "stop"
        total_hits = 0

        for _turn in range(_MAX_TOOL_TURNS):
            turn_tool_calls: list[ToolCall] = []
            async for ev in self._gateway.stream_tools(
                messages, tools=list(TOOL_SPECS), model=model
            ):
                if ev.text:
                    answer_parts.append(ev.text)
                    await self._publish(
                        state, envelopes.delta(state.stream_id, state.next_seq(), {"text": ev.text})
                    )
                if ev.tool_calls:
                    turn_tool_calls = list(ev.tool_calls)
                if ev.finish_reason:
                    finish_reason = ev.finish_reason
                if ev.usage is not None:
                    prompt_tokens += ev.usage.prompt_tokens
                    completion_tokens += ev.usage.completion_tokens
                    total_tokens += ev.usage.total_tokens

            if not turn_tool_calls:
                break

            # The assistant turn that requested tools must be in the transcript
            # before its tool results (provider protocol).
            messages.append(
                ChatMessage(role=Role.ASSISTANT, content="", tool_calls=tuple(turn_tool_calls))
            )
            for call in turn_tool_calls:
                outcome = await self._run_one_tool(
                    state=state,
                    retrieval=retrieval,
                    audit=audit,
                    call=call,
                    collection_ids=collection_ids,
                    default_k=_DEFAULT_K,
                )
                total_hits += outcome.hit_count
                messages.append(
                    ChatMessage(
                        role=Role.TOOL,
                        content=outcome.content,
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
                # Record + emit citations for each newly-seen permitted passage.
                for passage in outcome.passages:
                    if passage.chunk_id in cited:
                        continue
                    citation = GroundedCitation.from_passage(passage)
                    cited[passage.chunk_id] = citation

        # Persist the assistant message and its citations (INV-3): the citations
        # are exactly the permitted passages the tools returned — never more.
        answer_text = "".join(answer_parts).strip()
        if not answer_text:
            # The model produced no answer text (e.g. it only searched). Fall
            # back to an honest "couldn't find it" rather than an empty turn.
            answer_text = NO_SOURCES_FALLBACK
            await self._publish(
                state,
                envelopes.delta(state.stream_id, state.next_seq(), {"text": answer_text}),
            )

        stored_citations = await self._persist(
            session=session,
            tenant_id=tenant_id,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            model=model,
            content=answer_text,
            citations=list(cited.values()),
        )
        # Emit a citation event per persisted citation (now carrying its row id).
        # ``cited`` is already deduplicated by chunk_id, so each stored citation is
        # a distinct permitted passage — one event each, no extra guard needed.
        for citation in stored_citations:
            await self._emit_citation(state, citation)

        await self._audit_answer(
            audit=audit,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            question=question,
            model=model,
            citation_count=len(stored_citations),
            retrieved_hits=total_hits,
        )

        return _RunResult(
            finish_reason=finish_reason,
            citation_count=len(stored_citations),
            citations=tuple(stored_citations),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    async def _run_one_tool(
        self,
        *,
        state: _StreamState,
        retrieval: RetrievalService,
        audit: AuditSink,
        call: ToolCall,
        collection_ids: list[UUID] | None,
        default_k: int,
    ) -> ToolOutcome:
        """Surface a tool_call event, run the (permission-filtered) tool, audit it."""
        await self._publish(
            state,
            envelopes.event(
                state.stream_id,
                state.next_seq(),
                name="tool_call",
                data={"callId": call.id, "tool": call.name, "args": call.arguments},
            ),
        )
        outcome = await run_tool(
            retrieval,
            principal=self._principal,
            call=call,
            collection_ids=collection_ids,
            default_k=default_k,
        )
        # Audit every retrieval (INV-6): the permission-filtered search ran.
        await self._audit_retrieval(audit=audit, call=call, outcome=outcome)
        await self._publish(
            state,
            envelopes.event(
                state.stream_id,
                state.next_seq(),
                name="tool_result",
                data={
                    "callId": call.id,
                    "tool": call.name,
                    "hitCount": outcome.hit_count,
                    "summary": outcome.summary,
                },
            ),
        )
        return outcome

    # --- persistence + audit ------------------------------------------------

    async def _persist(
        self,
        *,
        session: AsyncSession,
        tenant_id: UUID,
        session_id: UUID,
        assistant_message_id: UUID,
        model: str,
        content: str,
        citations: list[GroundedCitation],
    ) -> list[GroundedCitation]:
        """Persist the assistant message + its citations; return citations w/ ids.

        The message row uses the pre-minted ``assistant_message_id`` (the same id
        that rode ``start``) so the WS ``messageId`` and the stored row agree. Each
        citation persists with the **source** char span (deep-link to the document,
        CC-11) and reloads carrying its row id for the citation events.
        """
        message_repo = MessageRepository(session, tenant_id)
        citation_repo = CitationRepository(session, tenant_id)
        await message_repo.add_with_id(
            message_id=assistant_message_id,
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content=content,
            model=model,
        )
        stored: list[GroundedCitation] = []
        for citation in citations:
            row = await citation_repo.add(
                message_id=assistant_message_id,
                chunk_id=citation.chunk_id,
                char_start=citation.char_start,
                char_end=citation.char_end,
                score=citation.score,
            )
            stored.append(
                GroundedCitation(
                    id=row.id,
                    document_id=citation.document_id,
                    document_name=citation.document_name,
                    chunk_id=citation.chunk_id,
                    snippet=citation.snippet,
                    char_start=citation.char_start,
                    char_end=citation.char_end,
                    score=citation.score,
                )
            )
        return stored

    async def _audit_retrieval(
        self, *, audit: AuditSink, call: ToolCall, outcome: ToolOutcome
    ) -> None:
        query = str(call.arguments.get("query") or call.arguments.get("name_or_query") or "")
        await audit.emit(
            action=AuditAction.RETRIEVAL_QUERY,
            actor=AuditActor.user(self._principal.user_id),
            resource_type="retrieval",
            resource_id=call.id,
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "tool": call.name,
                "query_hash": _hash_query(query),
                "document_ids": [str(d) for d in outcome.document_ids],
                "hit_count": outcome.hit_count,
            },
        )

    async def _audit_answer(
        self,
        *,
        audit: AuditSink,
        session_id: UUID,
        assistant_message_id: UUID,
        question: str,
        model: str,
        citation_count: int,
        retrieved_hits: int,
    ) -> None:
        await audit.emit(
            action=AuditAction.ANSWER_GENERATED,
            actor=AuditActor.user(self._principal.user_id),
            resource_type="message",
            resource_id=str(assistant_message_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "session_id": str(session_id),
                "model": model,
                "query_hash": _hash_query(question),
                "citation_count": citation_count,
                "retrieved_hits": retrieved_hits,
            },
        )

    # --- envelope helpers ---------------------------------------------------

    async def _emit_citation(self, state: _StreamState, citation: GroundedCitation) -> None:
        await self._publish(
            state,
            envelopes.event(
                state.stream_id,
                state.next_seq(),
                name="citation",
                data={
                    "id": str(citation.id),
                    "documentId": str(citation.document_id),
                    "documentName": citation.document_name,
                    "chunkId": str(citation.chunk_id),
                    "snippet": citation.snippet,
                    "charStart": citation.char_start,
                    "charEnd": citation.char_end,
                    **({"score": citation.score} if citation.score is not None else {}),
                },
            ),
        )

    async def _terminal_error(
        self, state: _StreamState, status: int, title: str, code: str, detail: str | None
    ) -> None:
        problem: dict[str, object] = {"title": title, "status": status, "code": code}
        if detail:
            problem["detail"] = detail
        await self._publish(state, envelopes.error(state.stream_id, state.next_seq(), problem))

    async def _publish(self, state: _StreamState, envelope: dict[str, object]) -> None:
        await self._backplane.publish(state.stream_id, envelope)


@dataclass(frozen=True, slots=True)
class _RunResult:
    """Internal result of the answer loop (feeds the ``done`` envelope)."""

    finish_reason: str
    citation_count: int
    citations: tuple[GroundedCitation, ...]
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


def _hash_query(query: str) -> str:
    """A stable, non-reversible hash of the query (audit metadata, spec 0004 §2.4).

    The audit log records a query **hash**, not the raw query, so the trail does
    not duplicate potentially sensitive question text while still letting a
    reviewer correlate retrieval + answer events for the same turn.
    """
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


__all__ = ["ChatRuntime"]
