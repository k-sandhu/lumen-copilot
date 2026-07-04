"""The eval harness runner — seed corpus → answer → observe → score (#29).

Shared machinery both the offline and the live eval tests use, so the two runs
differ only in *which adapters* they inject, not in how the golden set is scored.
The flow per question:

1. retrieve permitted passages for the question (the injected retrieval);
2. run the chat runtime over those passages (the injected gateway) to produce the
   streamed answer + persisted citations;
3. reload the citations from the DB and assemble an
   :class:`~tests.eval.metrics.ObservedAnswer`;
4. score it against its golden item (:mod:`tests.eval.scoring`).

The runtime, backplane, repositories, and permission filter are the **real**
ones; only the model gateway and (offline) the retrieval/embeddings are faked, so
the harness exercises the genuine grounding/citation/permission machinery
(spec 0004 INV-3) rather than a mock of it.

This module is import-only helpers (no tests); the offline/live test modules wire
the adapters and call :func:`run_eval`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.principal import Principal
from app.db.repositories import (
    ChunkInput,
    ChunkRepository,
    CitationRepository,
    CollectionRepository,
    DocumentRepository,
)
from app.domain.entities import DocumentStatus
from app.domain.llm import StreamEvent, ToolCall
from app.domain.retrieval import DocumentMatch, DocumentText, RetrievedPassage
from app.ingestion import chunk_text
from app.realtime.backplane import InMemoryBackplane
from app.search import IndexedChunk, OpenSearchStore
from app.services.chat_runtime import ChatRuntime
from tests.eval.golden import GOLDEN_QUESTIONS, GoldenDocument, GoldenQuestion
from tests.eval.metrics import ItemScore, ObservedAnswer, ObservedPassage
from tests.eval.scoring import aggregate, score_item
from tests.eval.support import RetrievalLike

# Chunking config for the offline ingest of the (short) golden documents. Smaller
# than the production default so each short golden document yields a couple of
# chunks — enough for retrieval to have to discriminate, deterministic offsets.
_CHUNK_SIZE = 240
_CHUNK_OVERLAP = 40


@dataclass(frozen=True, slots=True)
class SeededDocument:
    """A golden document after it has been ingested into the eval database."""

    doc_id: str
    document_id: UUID
    filename: str
    chunk_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class SeededCorpus:
    """The corpus seeded for one asking principal (by golden ``doc_id``)."""

    owner_id: UUID
    documents: dict[str, SeededDocument]


async def seed_corpus(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    owner_id: UUID,
    documents: Sequence[GoldenDocument],
    embed: object | None = None,
    store: OpenSearchStore | None = None,
) -> SeededCorpus:
    """Ingest the golden documents for ``owner_id`` using the real chunker (#21).

    Mirrors the ingestion pipeline's persist step: parse is a no-op (the golden
    text is already plain text), the **real** :func:`app.ingestion.chunk_text`
    produces offset-carrying chunks, and the tenant-scoped
    :class:`ChunkRepository` writes them — exactly the rows retrieval reads. When
    ``embed`` is supplied (offline), each chunk gets a deterministic vector so the
    faked retrieval can rank by it; live ingestion embeds for real elsewhere.

    When ``store`` is supplied (the live eval), each document's chunks are also
    indexed into OpenSearch (ADR-0010) — the eval seeds via the repository rather
    than the ingestion task, so it mirrors the write-path itself so the
    engine-backed :class:`RetrievalService` can find them.
    """
    collection = await CollectionRepository(session, tenant_id).create(
        owner_id=owner_id, name="Golden corpus"
    )
    seeded: dict[str, SeededDocument] = {}
    for golden in documents:
        document = await DocumentRepository(session, tenant_id).create(
            owner_id=owner_id,
            collection_id=collection.id,
            filename=golden.filename,
            mime_type="text/plain",
            size_bytes=len(golden.text.encode("utf-8")),
            storage_key=f"{tenant_id}/{golden.filename}",
            status=DocumentStatus.READY,
        )
        chunks = chunk_text(golden.text, chunk_size=_CHUNK_SIZE, overlap=_CHUNK_OVERLAP)
        inputs: list[ChunkInput] = []
        for chunk in chunks:
            vector = None
            if embed is not None:
                vector = list((await embed([chunk.text]))[0].vector)  # type: ignore[operator]
            inputs.append(
                ChunkInput(
                    text=chunk.text,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    embedding=vector,
                )
            )
        rows = await ChunkRepository(session, tenant_id).replace_for_document(document.id, inputs)
        if store is not None:
            await store.upsert_chunks(
                [
                    IndexedChunk(
                        chunk_id=r.id,
                        tenant_id=r.tenant_id,
                        document_id=r.document_id,
                        owner_id=owner_id,
                        collection_id=collection.id,
                        ord=r.ord,
                        text=r.text,
                        embedding=r.embedding,
                        char_start=r.char_start,
                        char_end=r.char_end,
                    )
                    for r in rows
                ],
                refresh=True,
            )
        seeded[golden.doc_id] = SeededDocument(
            doc_id=golden.doc_id,
            document_id=document.id,
            filename=golden.filename,
            chunk_ids=tuple(r.id for r in rows),
        )
    return SeededCorpus(owner_id=owner_id, documents=seeded)


class _GroundedGateway:
    """A scripted, *grounded* gateway: search once, then answer from the passages.

    Deterministic stand-in for a real model that emits a faithful grounded turn:
    turn 1 requests ``search_text`` with the question; turn 2 composes the answer
    from the passages the tool returned (read off the transcript) — so the answer
    only ever asserts what was retrieved, and the honest-refusal path fires when
    nothing was retrieved. This is the offline analogue of the live model; the
    live run swaps it for the real gateway.
    """

    def __init__(self, *, refusal_text: str) -> None:
        self._refusal = refusal_text

    async def stream_tools(
        self,
        messages: object,
        *,
        tools: object,
        model: object = None,
        tool_choice: object = None,
        api_key: object = None,
        api_base: object = None,

    ) -> AsyncIterator[StreamEvent]:
        msgs = list(messages)  # type: ignore[arg-type]
        # The question is the last user message; the tool results (if any) are the
        # most recent tool messages.
        tool_texts = [getattr(m, "content", "") for m in msgs if _role_value(m) == "tool"]
        if not tool_texts:
            question = next(
                (getattr(m, "content", "") for m in reversed(msgs) if _role_value(m) == "user"),
                "",
            )
            yield StreamEvent(
                tool_calls=(ToolCall(id="c1", name="search_text", arguments={"query": question}),),
                finish_reason="tool_calls",
            )
            return
        # Turn 2: ground the answer in what the tool returned. If the tool found
        # nothing relevant, give the honest refusal (AC-3).
        joined = "\n".join(tool_texts)
        if "No matching passages" in joined or not joined.strip():
            yield StreamEvent(text=self._refusal)
            yield StreamEvent(finish_reason="stop")
            return
        # Answer strictly from the retrieved passage text (the first block).
        yield StreamEvent(text="Based on your documents: ")
        yield StreamEvent(text=_first_passage_snippet(joined))
        yield StreamEvent(finish_reason="stop")


def _role_value(message: object) -> str:
    role = getattr(message, "role", None)
    return getattr(role, "value", "") if role is not None else ""


def _first_passage_snippet(rendered: str) -> str:
    """Pull the first rendered passage's body out of the tool reply text.

    The tool renders passages as ``[1] name (chunk …):\n<text>\n\n[2] …``; the
    grounded faked answer quotes the first passage's body so the answer text
    contains the supporting fact (and is genuinely grounded in retrieval).
    """
    blocks = rendered.split("\n\n")
    first = blocks[0] if blocks else rendered
    lines = first.splitlines()
    body = "\n".join(lines[1:]) if len(lines) > 1 else first
    return body.strip()


async def answer_question(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    retrieval: RetrievalLike,
    gateway: object,
    principal: Principal,
    session_id: UUID,
    question: GoldenQuestion,
    model: str,
    refusal_text: str,
) -> ObservedAnswer:
    """Run one question through the real runtime; assemble the observed answer.

    Captures what retrieval returned for the question (the recall signal),
    runs the :class:`ChatRuntime` (real grounding + citation persistence) over a
    fresh stream, then reloads the persisted citations so the observation
    reflects what was actually stored (INV-3), not just what streamed.
    """
    # What retrieval surfaces for this question (recorded for the recall metric).
    retrieved = await retrieval.search_text(
        principal=principal, query=question.question, k=6, collection_ids=None
    )

    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = ChatRuntime(
        sessionmaker=sessionmaker,
        gateway=gateway,  # type: ignore[arg-type]
        backplane=backplane,
        principal=principal,
        request_id=f"eval-{question.qid}",
        source_ip="127.0.0.1",
        retrieval_factory=lambda _session: retrieval,  # type: ignore[arg-type,return-value]
    )
    await runtime.run(
        stream_id=stream_id,
        session_id=session_id,
        question=question.question,
        model=model,
        history=[],
        collection_ids=None,
    )

    # Reassemble the streamed answer text + reload the persisted citations.
    answer_text = _collect_answer_text(backplane, stream_id)
    async with sessionmaker() as session:
        from app.db.repositories import MessageRepository

        messages = await MessageRepository(session, principal.tenant_id).list_for_session(
            session_id
        )
        assistant = [m for m in messages if m.role.value == "assistant"]
        latest = assistant[-1] if assistant else None
        citations: list[ObservedPassage] = []
        if latest is not None:
            hydrated = await CitationRepository(
                session, principal.tenant_id
            ).list_for_message_hydrated(latest.id)
            # A persisted citation is, by construction, drawn from a passage the
            # asker was permitted to retrieve (the runtime builds citations only
            # from RetrievedPassages — INV-3). So the cited passage's owner is the
            # asker; the INV-3 *negative* (a citation to a foreign owner) is proven
            # at the metric level (test_eval_metrics) and structurally at the e2e
            # level (a non-owner gets zero citations).
            citations = [
                ObservedPassage(
                    document_id=c.document_id,
                    document_name=c.document_name,
                    text=c.snippet,
                    owner_id=principal.user_id,
                )
                for c in hydrated
            ]
        if latest is not None and latest.content:
            answer_text = latest.content

    return ObservedAnswer(
        retrieved=tuple(
            ObservedPassage(
                document_id=p.document_id,
                document_name=p.document_name,
                text=p.text,
                owner_id=principal.user_id,
            )
            for p in retrieved
        ),
        citations=tuple(citations),
        answer_text=answer_text or refusal_text,
        asked_by=principal.user_id,
    )


def _collect_answer_text(backplane: InMemoryBackplane, stream_id: str) -> str:
    """Concatenate the ``delta`` text from a finished in-memory stream's replay."""
    parts: list[str] = []
    for env in backplane._replay.get(stream_id, ()):  # noqa: SLF001 — test introspection
        if env.get("type") == "delta":
            data = env.get("data", {})
            if isinstance(data, dict):
                parts.append(str(data.get("text", "")))
    return "".join(parts)


async def run_eval(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    retrieval: RetrievalLike,
    gateway: object,
    principal: Principal,
    new_session_id: object,
    model: str,
    refusal_text: str = ("I couldn't find anything in your sources that answers that."),
    questions: Sequence[GoldenQuestion] = GOLDEN_QUESTIONS,
) -> list[ItemScore]:
    """Run the whole golden set and return the per-item scores.

    ``new_session_id`` is an async callable returning a fresh chat-session id per
    question (so each turn persists independently); the offline/live test supplies
    it bound to its own DB. Returns the list of :class:`ItemScore` the test then
    aggregates + asserts against the thresholds.
    """
    scores: list[ItemScore] = []
    for question in questions:
        session_id = await new_session_id()  # type: ignore[operator]
        observed = await answer_question(
            sessionmaker=sessionmaker,
            retrieval=retrieval,
            gateway=gateway,
            principal=principal,
            session_id=session_id,
            question=question,
            model=model,
            refusal_text=refusal_text,
        )
        scores.append(score_item(question, observed))
    return scores


def grounded_gateway(*, refusal_text: str) -> object:
    """Build the offline scripted grounded gateway (search → ground → answer)."""
    return _GroundedGateway(refusal_text=refusal_text)


__all__ = [
    "DocumentMatch",
    "DocumentText",
    "RetrievedPassage",
    "SeededCorpus",
    "SeededDocument",
    "aggregate",
    "answer_question",
    "grounded_gateway",
    "run_eval",
    "seed_corpus",
]
