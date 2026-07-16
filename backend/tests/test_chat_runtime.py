"""Chat runtime tests — the agentic grounded answer loop (CC-6 #24 / CC-11 #26).

Drives :class:`~app.services.chat_runtime.ChatRuntime` end-to-end **offline**: a
scripted fake gateway emits the tool-aware stream (text + tool calls), a fake
retrieval returns permitted passages, and the in-memory backplane captures the
published envelopes. The real agentic loop (a live model deciding to search) needs
an OPENROUTER_API_KEY + the running stack — these tests fix the *plumbing*: the
exact WS lifecycle, grounding/citation persistence (INV-3), the honest
no-source path, and the terminal-error contract.

Headlines:
* full lifecycle: start → tool_call → tool_result → citation → delta → done,
  with a single terminal and a monotonic seq;
* citations are persisted to the assistant message and reload via the repo
  (CC-11 AC-2) — and only ever describe retrieved (permitted) passages (INV-3);
* a turn that retrieved nothing yields a zero-citation answer, shown honestly
  (no fabricated reference) — issue #24 AC-3;
* a gateway failure ends the stream with exactly one terminal ``error`` (and no
  vendor error leaks).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.db.base import Base
from app.db.repositories import (
    ChatSessionRepository,
    CitationRepository,
    MessageRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import CodeRunStatus, Role
from app.domain.llm import StreamEvent, ToolCall
from app.domain.retrieval import DocumentMatch, DocumentText, RetrievedPassage
from app.realtime.backplane import InMemoryBackplane
from app.services.chat_runtime import ChatRuntime

import app.db.models  # noqa: F401  isort: skip


# --- Fakes ------------------------------------------------------------------


class _ScriptedGateway:
    """A gateway whose ``stream_tools`` replays a scripted list of turns.

    Each turn is a list of ``StreamEvent``s (text chunks then a terminal event
    carrying ``tool_calls`` / ``finish_reason``). Consecutive *tool-choice=auto*
    ``stream_tools`` calls pop successive turns, so a tool-call turn followed by a
    final-answer turn drives the loop exactly once.

    When the runtime exhausts its tool-turn budget it makes one **forced**
    ``stream_tools`` call with ``tool_choice="none"`` to synthesise a tool-free
    answer (issue #148). This fake models a real provider's response to that:
    such a call yields the ``synthesis`` script (a tool-free turn) instead of
    advancing the ``turns`` list — so a script of all-tool turns still ends with
    a clean synthesised answer. ``auto_calls`` / ``synthesis_calls`` are exposed
    so a test can assert exactly how many tool turns ran vs the forced final one.
    """

    def __init__(
        self,
        turns: list[list[StreamEvent]],
        *,
        synthesis: list[StreamEvent] | None = None,
    ) -> None:
        self._turns = turns
        self._synthesis = synthesis
        self.calls = 0
        self.auto_calls = 0
        self.synthesis_calls = 0

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
        self.calls += 1
        if tool_choice == "none":
            self.synthesis_calls += 1
            turn = (
                self._synthesis
                if self._synthesis is not None
                else [StreamEvent(finish_reason="stop")]
            )
        else:
            turn = self._turns[min(self.auto_calls, len(self._turns) - 1)]
            self.auto_calls += 1
        for ev in turn:
            yield ev


class _BoomGateway:
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
        raise RuntimeError("provider exploded")
        yield  # pragma: no cover — unreachable, makes this an async generator


class _FakeRetrieval:
    """A retrieval stand-in returning fixed permitted passages for search_text."""

    def __init__(self, passages: list[RetrievedPassage]) -> None:
        self._passages = passages
        self.queries: list[str] = []

    async def search_text(
        self, *, principal: object, query: str, k: int, collection_ids: object = None
    ) -> list[RetrievedPassage]:
        self.queries.append(query)
        return list(self._passages)

    async def search_documents(
        self, *, principal: object, name_or_query: str, k: int = 10
    ) -> list[DocumentMatch]:
        return []

    async def get_document(self, *, principal: object, document_id: object) -> DocumentText | None:
        return None


def _passage(document_id: uuid.UUID, chunk_id: uuid.UUID, doc_name: str) -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=chunk_id,
        document_id=document_id,
        document_name=doc_name,
        ord=0,
        text="The standard deduction for 2024 is $14,600.",
        char_start=100,
        char_end=143,
        score=0.91,
    )


# --- Fixture: SQLite + a seeded tenant/user/session/document/chunk ----------


class _Ctx:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        principal: Principal,
        session_id: uuid.UUID,
        document_id: uuid.UUID,
        chunk_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.principal = principal
        self.session_id = session_id
        self.document_id = document_id
        self.chunk_id = chunk_id
        self.tenant_id = tenant_id


@pytest_asyncio.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as seed:
            tenant = await TenantRepository(seed).create(name="Acme")
            user = await UserRepository(seed, tenant.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            from app.db.repositories import (
                ChunkInput,
                ChunkRepository,
                CollectionRepository,
                DocumentRepository,
            )

            coll = await CollectionRepository(seed, tenant.id).create(owner_id=user.id, name="c")
            doc = await DocumentRepository(seed, tenant.id).create(
                owner_id=user.id,
                collection_id=coll.id,
                filename="taxes.pdf",
                mime_type="application/pdf",
                size_bytes=10,
                storage_key=f"{tenant.id}/taxes.pdf",
            )
            chunks = await ChunkRepository(seed, tenant.id).replace_for_document(
                doc.id,
                [
                    ChunkInput(
                        text="The standard deduction for 2024 is $14,600.",
                        char_start=100,
                        char_end=143,
                    )
                ],
            )
            session = await ChatSessionRepository(seed, tenant.id).create(
                owner_id=user.id, model="anthropic/claude-opus-4.8", title="t"
            )
            await seed.commit()
            yield _Ctx(
                sessionmaker=factory,
                principal=Principal(user_id=user.id, tenant_id=tenant.id, roles=(Role.MEMBER,)),
                session_id=session.id,
                document_id=doc.id,
                chunk_id=chunks[0].id,
                tenant_id=tenant.id,
            )
    finally:
        await engine.dispose()


def _runtime(
    ctx: _Ctx,
    *,
    gateway: object,
    retrieval: object,
    backplane: InMemoryBackplane,
    default_max_tool_turns: int = 4,
    context_config: object = None,
) -> ChatRuntime:
    return ChatRuntime(
        sessionmaker=ctx.sessionmaker,
        gateway=gateway,  # type: ignore[arg-type]
        backplane=backplane,
        principal=ctx.principal,
        request_id="req-1",
        source_ip="127.0.0.1",
        default_max_tool_turns=default_max_tool_turns,
        context_config=context_config,  # type: ignore[arg-type]
        retrieval_factory=lambda _session: retrieval,  # type: ignore[arg-type,return-value]
    )


async def _drain(backplane: InMemoryBackplane, stream_id: str) -> list[dict[str, object]]:
    return [env async for env in backplane.subscribe(stream_id)]


# --- Full lifecycle: search → ground → cite → answer → done -----------------


async def test_grounded_answer_full_lifecycle(ctx: _Ctx) -> None:
    passage = _passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")
    retrieval = _FakeRetrieval([passage])
    gateway = _ScriptedGateway(
        [
            # Turn 1: the model calls search_text.
            [
                StreamEvent(
                    tool_calls=(
                        ToolCall(id="c1", name="search_text", arguments={"query": "deduction"}),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            # Turn 2: the model answers using the retrieved passage.
            [
                StreamEvent(text="The 2024 standard deduction "),
                StreamEvent(text="is $14,600."),
                StreamEvent(finish_reason="stop"),
            ],
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex

    import asyncio

    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(ctx, gateway=gateway, retrieval=retrieval, backplane=backplane)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="What is the 2024 standard deduction?",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    types = [e["type"] for e in envs]
    assert types[0] == "start"
    assert types[-1] == "done"
    assert types.count("done") == 1 and "error" not in types
    # Monotonic seq.
    seqs = [e["seq"] for e in envs]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # The event vocabulary all appeared.
    names = [e.get("name") for e in envs if e["type"] == "event"]
    assert "tool_call" in names
    assert "tool_result" in names
    assert "citation" in names
    # Tokens streamed.
    text = "".join(e["data"]["text"] for e in envs if e["type"] == "delta")  # type: ignore[index]
    assert "14,600" in text
    # done summary reports one citation.
    done = envs[-1]
    assert done["data"]["citationCount"] == 1  # type: ignore[index]


async def test_citation_persisted_and_reloadable(ctx: _Ctx) -> None:
    passage = _passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")
    retrieval = _FakeRetrieval([passage])
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(ToolCall(id="c1", name="search_text", arguments={"query": "q"}),),
                    finish_reason="tool_calls",
                )
            ],
            [StreamEvent(text="Grounded answer."), StreamEvent(finish_reason="stop")],
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(ctx, gateway=gateway, retrieval=retrieval, backplane=backplane)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )

    # The assistant message + its citation persisted; reload via the repos.
    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assistant = [m for m in messages if m.role.value == "assistant"]
        assert len(assistant) == 1
        assert assistant[0].content == "Grounded answer."
        assert assistant[0].model == "anthropic/claude-opus-4.8"
        citations = await CitationRepository(session, ctx.tenant_id).list_for_message_hydrated(
            assistant[0].id
        )
    assert len(citations) == 1
    cit = citations[0]
    # The citation resolves to the *retrieved* (permitted) passage (INV-3).
    assert cit.chunk_id == ctx.chunk_id
    assert cit.document_id == ctx.document_id
    assert cit.document_name == "taxes.pdf"
    # Source-document offsets (deep-link target, CC-11 AC-3).
    assert (cit.char_start, cit.char_end) == (100, 143)


async def test_zero_citation_answer_is_honest(ctx: _Ctx) -> None:
    # The model searched, retrieval found nothing, and the model gives the
    # honest "couldn't find it" answer — a zero-citation answer, shown as such.
    retrieval = _FakeRetrieval([])
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(ToolCall(id="c1", name="search_text", arguments={"query": "q"}),),
                    finish_reason="tool_calls",
                )
            ],
            [
                StreamEvent(text="I couldn't find anything in your sources that answers that."),
                StreamEvent(finish_reason="stop"),
            ],
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(ctx, gateway=gateway, retrieval=retrieval, backplane=backplane)

    import asyncio

    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    # No citation event; done reports zero citations; the answer is the honest one.
    assert "citation" not in [e.get("name") for e in envs if e["type"] == "event"]
    assert envs[-1]["data"]["citationCount"] == 0  # type: ignore[index]
    async with ctx.sessionmaker() as session:
        msgs = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assistant = [m for m in msgs if m.role.value == "assistant"][0]
        cits = await CitationRepository(session, ctx.tenant_id).list_for_message(assistant.id)
    assert cits == []
    assert "couldn't find" in assistant.content.lower()


async def test_empty_model_answer_falls_back_to_honest_message(ctx: _Ctx) -> None:
    # The model only searched and never produced answer text — the runtime fills
    # in the honest fallback rather than persisting an empty assistant turn.
    retrieval = _FakeRetrieval([])
    gateway = _ScriptedGateway([[StreamEvent(finish_reason="stop")]])
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(ctx, gateway=gateway, retrieval=retrieval, backplane=backplane)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    async with ctx.sessionmaker() as session:
        msgs = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assistant = [m for m in msgs if m.role.value == "assistant"][0]
    assert assistant.content.strip() != ""


def _tool_turn(narration: str, call_id: str) -> list[StreamEvent]:
    """A turn that emits a little narration then requests a search (never answers)."""
    return [
        StreamEvent(text=narration),
        StreamEvent(
            tool_calls=(ToolCall(id=call_id, name="search_text", arguments={"query": "q"}),),
            finish_reason="tool_calls",
        ),
    ]


async def test_tool_budget_exhaustion_forces_a_synthesized_answer(ctx: _Ctx) -> None:
    """Issue #148 regression: if every turn calls a tool until the budget is spent,
    the runtime forces one tool-free synthesis so the answer is real, not narration.

    Without the fix the loop exits via the turn cap with ``answer_text`` equal to
    just the inter-tool narration ("I'll search…", "Let me read…") — the live
    2026-06-24 empty-answer bug. The fix makes a final ``tool_choice="none"`` call
    and persists *that* as the answer.
    """
    passage = _passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")
    retrieval = _FakeRetrieval([passage])
    gateway = _ScriptedGateway(
        # Every scripted turn requests a tool (with leading narration) — the model
        # never volunteers a tool-free answer within the budget.
        [
            _tool_turn("I'll search your documents for the incident. ", "c1"),
            _tool_turn("Let me read the full postmortem. ", "c2"),
        ],
        # The forced tool-free synthesis the runtime demands once the budget is spent.
        synthesis=[
            StreamEvent(text="The root cause was a bad index migration; "),
            StreamEvent(text="action items: add a canary and a rollback runbook."),
            StreamEvent(finish_reason="stop"),
        ],
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex

    import asyncio

    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(
        ctx, gateway=gateway, retrieval=retrieval, backplane=backplane, default_max_tool_turns=2
    )
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="What was the root cause and the action items?",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    # The loop spent its 2-turn budget on tool calls, then forced exactly one
    # tool-free synthesis (tool_choice="none").
    assert gateway.auto_calls == 2
    assert gateway.synthesis_calls == 1
    # Terminal is a single done (no error), and it is NOT the empty-answer fallback.
    assert envs[-1]["type"] == "done"
    assert [e["type"] for e in envs].count("done") == 1 and "error" not in [e["type"] for e in envs]

    expected = (
        "The root cause was a bad index migration; "
        "action items: add a canary and a rollback runbook."
    )
    # Regression (PR #150 review): the LIVE stream must equal the synthesized answer
    # too. The inter-tool narration is never emitted as a delta, so the streamed
    # answer and the stored message agree — no live/persisted divergence.
    streamed = "".join(e["data"]["text"] for e in envs if e["type"] == "delta")  # type: ignore[index]
    assert streamed == expected
    assert "I'll search" not in streamed
    assert "Let me read" not in streamed

    async with ctx.sessionmaker() as session:
        msgs = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assistant = [m for m in msgs if m.role.value == "assistant"][0]
    # The persisted answer is the synthesized answer — not the inter-tool narration.
    assert assistant.content == expected


async def test_per_tenant_override_caps_tool_turns(ctx: _Ctx) -> None:
    """A tenant's ``max_tool_turns`` override bounds the loop, beating the default (#148).

    The default budget is set high (20); the tenant override is 1. If the override
    were ignored the all-tool script would drive the loop 20 times (the fake clamps
    to its last tool turn); respecting it runs exactly **one** tool turn before the
    forced synthesis. ``auto_calls == 1`` is the proof the override won.
    """
    # Set the per-tenant override to 1 before the answer runs.
    async with ctx.sessionmaker() as session:
        await TenantRepository(session).update(ctx.tenant_id, max_tool_turns=1)
        await session.commit()

    passage = _passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")
    retrieval = _FakeRetrieval([passage])
    gateway = _ScriptedGateway(
        [_tool_turn("Searching… ", "c1")],
        synthesis=[StreamEvent(text="Grounded final answer."), StreamEvent(finish_reason="stop")],
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx, gateway=gateway, retrieval=retrieval, backplane=backplane, default_max_tool_turns=20
    )

    import asyncio

    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    # Exactly one tool turn ran (the override of 1), then the forced synthesis —
    # not the default of 20.
    assert gateway.auto_calls == 1
    assert gateway.synthesis_calls == 1
    # The streamed answer equals the synthesis (narration suppressed) and matches
    # the persisted message — no live/stored divergence (PR #150 review).
    streamed = "".join(e["data"]["text"] for e in envs if e["type"] == "delta")  # type: ignore[index]
    assert streamed == "Grounded final answer."
    assert "Searching" not in streamed
    async with ctx.sessionmaker() as session:
        msgs = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assistant = [m for m in msgs if m.role.value == "assistant"][0]
    assert assistant.content == "Grounded final answer."


async def test_gateway_failure_ends_with_single_terminal_error(ctx: _Ctx) -> None:
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx, gateway=_BoomGateway(), retrieval=_FakeRetrieval([]), backplane=backplane
    )

    import asyncio

    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    assert envs[0]["type"] == "start"
    assert envs[-1]["type"] == "error"
    # Exactly one terminal; the vendor message never leaks into the problem.
    assert [e["type"] for e in envs].count("error") == 1
    assert "done" not in [e["type"] for e in envs]
    problem = envs[-1]["problem"]  # type: ignore[index]
    assert problem["status"] == 500
    assert "exploded" not in str(problem)


async def test_retrieval_and_answer_audit_events_emitted(ctx: _Ctx) -> None:
    passage = _passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")
    retrieval = _FakeRetrieval([passage])
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(ToolCall(id="c1", name="search_text", arguments={"query": "q"}),),
                    finish_reason="tool_calls",
                )
            ],
            [StreamEvent(text="A."), StreamEvent(finish_reason="stop")],
        ]
    )
    backplane = InMemoryBackplane()
    runtime = _runtime(ctx, gateway=gateway, retrieval=retrieval, backplane=backplane)
    await runtime.run(
        stream_id=uuid.uuid4().hex,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    from app.db.repositories import AuditEventRepository

    async with ctx.sessionmaker() as session:
        events = await AuditEventRepository(session, ctx.tenant_id).list_recent()
    actions = {e.action for e in events}
    assert "retrieval.query" in actions
    assert "answer.generated" in actions
    answer_ev = next(e for e in events if e.action == "answer.generated")
    assert answer_ev.metadata["citation_count"] == 1
    # The distinct cited document id is recorded on the answer event (#249) so
    # the audit read-path can synthesise an allow-candidate and the "Answers
    # cited" KPI counts this grounded answer as cited.
    assert answer_ev.metadata["document_ids"] == [str(ctx.document_id)]


# ---------------------------------------------------------------------------
# run_python seam wiring (issue #231) — deny-by-default + the streamed events.
# ---------------------------------------------------------------------------


class _RecordingSandboxFactory:
    """A sandbox factory that records whether it was invoked + returns a fake seam.

    Lets the runtime wiring tests assert deny-by-default: the factory is called ONLY
    when a run's allow-list offers ``run_python`` (below), never for an ad-hoc
    session on the default allow-list.
    """

    def __init__(self, seam: object) -> None:
        self._seam = seam
        self.contexts: list[object] = []

    def __call__(self, sandbox_ctx: object) -> object:
        self.contexts.append(sandbox_ctx)
        return self._seam


class _FakeSeam:
    """A ``SandboxToolRunner`` that records the submission and returns a scripted run.

    The seam's own WS-event emission is exercised end-to-end by the real
    ``ChatSandboxToolRunner`` in ``test_sandbox_tool_runner.py``; this fake only proves
    the runtime *wiring* — that a ``run_python`` call reaches the seam when the tool
    is allowed.
    """

    def __init__(self, sandbox_ctx: object) -> None:
        self._ctx = sandbox_ctx
        self.run_id = uuid.uuid4()
        self.submissions: list[str] = []

    async def submit(self, *, code: str, timeout_s: int | None = None) -> object:
        from app.services.tools.types import SandboxRun

        self.submissions.append(code)
        return SandboxRun(
            code_run_id=self.run_id,
            status=CodeRunStatus.SUCCEEDED,
            exit_code=0,
            duration_ms=5,
            stdout="hi\n",
            stderr="",
            artifact_ids=(),
        )


def _sandbox_runtime(
    ctx: _Ctx,
    *,
    gateway: object,
    retrieval: object,
    backplane: InMemoryBackplane,
    sandbox_factory: object,
) -> ChatRuntime:
    return ChatRuntime(
        sessionmaker=ctx.sessionmaker,
        gateway=gateway,  # type: ignore[arg-type]
        backplane=backplane,
        principal=ctx.principal,
        request_id="req-1",
        source_ip="127.0.0.1",
        default_max_tool_turns=4,
        retrieval_factory=lambda _session: retrieval,  # type: ignore[arg-type,return-value]
        sandbox_factory=sandbox_factory,  # type: ignore[arg-type]
    )


async def test_run_python_seam_wired_when_allowed_but_gated_by_approval(ctx: _Ctx) -> None:
    """The seam is wired when a run offers run_python — but INV-7 still gates it.

    ``run_python`` is a T2 tool: the governed runner routes it through the approval
    gate BEFORE the handler runs, and the chat runtime wires the fail-closed default
    (``DenyAllApprovalGate`` — no in-session approval flow ships yet, F-ADMIN-TOOLS).
    So the seam is BUILT (run_python is in the allow-list) yet the call is
    **approval_denied** and the seam's ``submit`` is never reached — the consequential
    action does not execute without a recorded approval (the negative AC).
    """
    from app.services.assistant_runtime import AssistantRunConfig
    from app.services.prompts import GROUNDED_SYSTEM_PROMPT

    seam_holder: dict[str, _FakeSeam] = {}

    def _factory(sandbox_ctx: object) -> object:
        seam = _FakeSeam(sandbox_ctx)
        seam_holder["seam"] = seam
        return seam

    gateway = _ScriptedGateway(
        [
            # Turn 1: the model calls run_python.
            [
                StreamEvent(
                    tool_calls=(
                        ToolCall(id="c1", name="run_python", arguments={"code": "print(1)"}),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            # Turn 2: the model answers (having read the denial).
            [StreamEvent(text="I could not run the code."), StreamEvent(finish_reason="stop")],
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex

    import asyncio

    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _sandbox_runtime(
        ctx, gateway=gateway, retrieval=_FakeRetrieval([]), backplane=backplane,
        sandbox_factory=_factory,
    )
    # An assistant config whose allow-list grants run_python (admin/assistant-gated).
    assistant_config = AssistantRunConfig(
        system_prompt=GROUNDED_SYSTEM_PROMPT,
        allowed=frozenset({"run_python"}),
        collection_ids=None,
        model=None,
    )
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="compute 1",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
        assistant_config=assistant_config,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    # The seam was BUILT (run_python was in the allow-list) — the wiring is present.
    assert "seam" in seam_holder
    # ...but the approval gate blocked the call, so the seam's submit was NOT reached
    # (INV-7: no unapproved consequential action executes).
    assert seam_holder["seam"].submissions == []
    # The tool_result event reports the governance denial the model reads.
    tool_results = [
        e for e in envs if e["type"] == "event" and e.get("name") == "tool_result"
    ]
    assert any(
        e["data"]["tool"] == "run_python" and e["data"]["ok"] is False  # type: ignore[index]
        for e in tool_results
    )


async def test_ad_hoc_session_never_wires_run_python_seam(ctx: _Ctx) -> None:
    """Deny-by-default: an ad-hoc session (default allow-list) never builds the seam."""
    factory = _RecordingSandboxFactory(seam=object())
    # The model tries to call run_python, but ad-hoc chat does not offer it, so the
    # governed runner denies it (not_permitted) and the seam factory is never invoked.
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(
                        ToolCall(id="c1", name="run_python", arguments={"code": "print(1)"}),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [StreamEvent(text="No code ran."), StreamEvent(finish_reason="stop")],
        ]
    )
    backplane = InMemoryBackplane()
    runtime = _sandbox_runtime(
        ctx, gateway=gateway, retrieval=_FakeRetrieval([]), backplane=backplane,
        sandbox_factory=factory,
    )
    await runtime.run(
        stream_id=uuid.uuid4().hex,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    # The factory was NEVER invoked — no execution plumbing on a path that can't use it.
    assert factory.contexts == []


# ---------------------------------------------------------------------------
# Custom-instructions injection (user settings) — composed BEFORE grounding.
# ---------------------------------------------------------------------------


class _CapturingGateway:
    """A gateway that records the ``messages`` of the first ``stream_tools`` call.

    Answers in a single tool-free turn so the run reaches the model exactly once with
    the composed system prompt as ``messages[0]`` — which the test inspects to assert
    the custom-instructions → grounding ordering.
    """

    def __init__(self) -> None:
        self.system_prompt: str | None = None
        self.first_messages: list[object] | None = None

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
        if self.system_prompt is None and isinstance(messages, list) and messages:
            # The runtime places the composed system prompt as the first ChatMessage.
            self.system_prompt = messages[0].content
            self.first_messages = list(messages)
        yield StreamEvent(text="ok")
        yield StreamEvent(finish_reason="stop")


async def test_custom_instructions_prepended_before_grounding(ctx: _Ctx) -> None:
    """The user's custom instructions lead the system prompt; grounding still follows."""
    from app.services.prompts import GROUNDED_SYSTEM_PROMPT

    gateway = _CapturingGateway()
    backplane = InMemoryBackplane()
    runtime = _runtime(ctx, gateway=gateway, retrieval=_FakeRetrieval([]), backplane=backplane)
    instructions = "You are Alice's tax assistant. Always be concise."
    await runtime.run(
        stream_id=uuid.uuid4().hex,
        session_id=ctx.session_id,
        question="hello",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
        custom_instructions=instructions,
    )

    prompt = gateway.system_prompt
    assert prompt is not None
    # The custom instructions appear …
    assert instructions in prompt
    # … AND the grounding contract still follows (INV-3 preserved) …
    assert GROUNDED_SYSTEM_PROMPT in prompt
    # … in that order (instructions first, then grounding).
    assert prompt.index(instructions) < prompt.index(GROUNDED_SYSTEM_PROMPT)


async def test_no_custom_instructions_leaves_bare_grounded_prompt(ctx: _Ctx) -> None:
    """Ad-hoc chat with no custom instructions uses the grounded prompt unchanged."""
    from app.services.prompts import GROUNDED_SYSTEM_PROMPT

    gateway = _CapturingGateway()
    backplane = InMemoryBackplane()
    runtime = _runtime(ctx, gateway=gateway, retrieval=_FakeRetrieval([]), backplane=backplane)
    await runtime.run(
        stream_id=uuid.uuid4().hex,
        session_id=ctx.session_id,
        question="hello",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
        custom_instructions=None,
    )
    assert gateway.system_prompt == GROUNDED_SYSTEM_PROMPT


async def test_context_assembler_trims_history_end_to_end(ctx: _Ctx) -> None:
    """#410: the runtime assembles under a budget — a tiny window trims old history.

    Proves the assembler is actually wired into the runtime (not just unit-tested):
    with a small injected ``ContextConfig`` fallback window and no model in the
    map, the oldest turns are dropped before the prompt reaches the gateway, so
    the captured ``messages`` carry only the newest history + the question.
    """
    from app.domain.llm import ChatMessage
    from app.domain.llm import Role as LlmRole
    from app.llm.context import ContextConfig

    gateway = _CapturingGateway()
    backplane = InMemoryBackplane()
    # A moderate window (fallback 6000 − 1024 margin ⇒ ~4976 budget) that easily
    # holds the fixed segments (grounded prompt + tool schemas + question) but is
    # dwarfed by the two ENORMOUS ancient turns below — so the assembler trims
    # them and keeps the tiny recent turn, rather than refusing. The unknown model
    # id (not in the litellm map) exercises the fallback resolver (AC-3) too.
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
        context_config=ContextConfig(fallback_max_input_tokens=6000, output_headroom_tokens=0),
    )
    long_history = [
        ChatMessage(role=LlmRole.USER, content="ancient question " * 3000),
        ChatMessage(role=LlmRole.ASSISTANT, content="ancient answer " * 3000),
        ChatMessage(role=LlmRole.USER, content="recent question"),
    ]
    await runtime.run(
        stream_id=uuid.uuid4().hex,
        session_id=ctx.session_id,
        question="the new question",
        model="some/unknown-model-not-in-map",
        history=long_history,
        collection_ids=None,
    )
    assert gateway.first_messages is not None
    contents = [m.content for m in gateway.first_messages]  # type: ignore[attr-defined]
    # The ancient, oversized turns were dropped; the new question is always last;
    # the grounded system prompt still leads (segment order preserved).
    assert "ancient question " * 3000 not in contents
    assert "ancient answer " * 3000 not in contents
    assert contents[-1] == "the new question"
    assert contents[0].startswith("You are Lumen Copilot")


class _RecordingSearchGateway:
    """Records the FULL wire estimate of every ``messages`` + ``tools`` payload.

    Drives the loop so tool results accumulate (#424 review, finding 1). Records
    each turn's estimate via the SAME assembler metric the guard uses (message
    wire form + tool schemas + framing) — not just content bytes — so a test can
    prove no over-budget payload is ever sent, and would FAIL if the guard were
    removed. Records whether the turn was a forced synthesis (tool_choice="none").
    """

    def __init__(self) -> None:
        self.estimates: list[int] = []
        self.synthesis_estimates: list[int] = []

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
        from app.llm.context import estimate_message_tokens

        assert isinstance(messages, list)
        assert isinstance(tools, list)
        est = estimate_message_tokens(
            messages, tools, counter=lambda t: len(t.encode("utf-8"))
        )
        self.estimates.append(est)
        if tool_choice == "none":
            self.synthesis_estimates.append(est)
            yield StreamEvent(text="final")
            yield StreamEvent(finish_reason="stop")
            return
        yield StreamEvent(
            tool_calls=(ToolCall(id="c1", name="search_text", arguments={"query": "x"}),),
            finish_reason="tool_calls",
        )


async def test_oversized_tool_results_never_exceed_budget(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#424 review, finding 1: with huge tool results and a tiny window, the loop
    never sends an over-budget payload — it refuses deterministically instead."""
    import asyncio
    import sys
    import types as _types

    from app.llm.context import ContextConfig

    # Force the assembler's counting/resolver onto their deterministic fallbacks
    # (conservative UTF-8 bytes; the configured fallback window) by making litellm
    # unavailable — so the token budget is measured in BYTES and the assertion
    # below is exact rather than dependent on a real tokenizer.
    def _raise(**_kw: object) -> int:
        raise RuntimeError("no tokenizer in test")

    fake_litellm = _types.SimpleNamespace(token_counter=_raise, get_model_info=_raise)
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)  # type: ignore[attr-defined]

    # A retrieval that returns a passage far larger than the window.
    huge = RetrievedPassage(
        chunk_id=ctx.chunk_id,
        document_id=ctx.document_id,
        document_name="big.pdf",
        ord=0,
        text="P" * 8000,
        char_start=0,
        char_end=8000,
        score=0.9,
    )
    gateway = _RecordingSearchGateway()
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    # fallback 5024 − 0 headroom − 1024 margin ⇒ budget 4000 bytes. The initial
    # [system, question] + tool schemas fits, but the accumulating tool results
    # push a later turn over budget — where the guard must refuse, not send.
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([huge]),
        backplane=backplane,
        context_config=ContextConfig(fallback_max_input_tokens=5024, output_headroom_tokens=0),
    )
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="summarize the big doc",
        model="some/unknown-model-not-in-map",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    budget = 5024 - 1024
    # Every payload the gateway received — measured by the SAME full-wire estimate
    # the guard uses — was within budget. Without the guard, the accumulating tool
    # results would push a later estimate over budget and this would FAIL.
    assert gateway.estimates  # the loop ran at least once (initial fit)
    assert all(e <= budget for e in gateway.estimates), gateway.estimates
    # And the guard actually fired: the run terminated with the typed refusal
    # rather than sending an over-budget call.
    types = [e["type"] for e in envs]
    assert types.count("done") + types.count("error") == 1
    terminal = envs[-1]
    assert terminal["type"] == "error"
    assert terminal["problem"]["code"] == "context_too_large"  # type: ignore[index]


async def test_forced_synthesis_payload_is_budget_guarded(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#424 third re-review: with a single tool turn, the FORCED-SYNTHESIS call
    must also be budget-guarded — a huge result gathered on the one turn cannot
    be sent unchecked at synthesis time."""
    import asyncio
    import sys
    import types as _types

    from app.llm.context import ContextConfig

    def _raise(**_kw: object) -> int:
        raise RuntimeError("no tokenizer in test")

    fake = _types.SimpleNamespace(token_counter=_raise, get_model_info=_raise)
    monkeypatch.setitem(sys.modules, "litellm", fake)  # type: ignore[arg-type]

    huge = RetrievedPassage(
        chunk_id=ctx.chunk_id,
        document_id=ctx.document_id,
        document_name="big.pdf",
        ord=0,
        text="Q" * 8000,
        char_start=0,
        char_end=8000,
        score=0.9,
    )
    gateway = _RecordingSearchGateway()
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    # max_tool_turns=1: one tool turn, then the loop is exhausted and the runtime
    # makes the forced-synthesis call. Budget 4000 (5024 − 1024); the one huge
    # result makes the synthesis payload oversized ⇒ must be refused, not sent.
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([huge]),
        backplane=backplane,
        default_max_tool_turns=1,
        context_config=ContextConfig(fallback_max_input_tokens=5024, output_headroom_tokens=0),
    )
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="summarize the big doc",
        model="some/unknown-model-not-in-map",
        history=[],
        collection_ids=None,
    )
    await asyncio.wait_for(consumer, timeout=2.0)
    budget = 5024 - 1024
    # No payload (ordinary or forced-synthesis) exceeded budget.
    assert all(e <= budget for e in gateway.estimates), gateway.estimates


# --- #409: token & cache usage — reported on done, recorded per answer -------


async def test_usage_summed_reported_and_recorded_with_cache_fields(ctx: _Ctx) -> None:
    """#409 AC-1/AC-2: ``done.usage`` carries the cache fields summed across turns,
    and exactly one ``llm_usage`` row lands, linked to the session + assistant message."""
    import asyncio

    from app.db.repositories import LlmUsageRepository
    from app.domain.llm import TokenUsage

    passage = _passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")
    retrieval = _FakeRetrieval([passage])
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(
                        ToolCall(id="c1", name="search_text", arguments={"query": "deduction"}),
                    ),
                    finish_reason="tool_calls",
                    usage=TokenUsage(
                        prompt_tokens=100,
                        completion_tokens=10,
                        total_tokens=110,
                        cached_prompt_tokens=0,
                        cache_write_tokens=80,
                    ),
                ),
            ],
            [
                StreamEvent(text="The 2024 standard deduction is $14,600."),
                StreamEvent(
                    finish_reason="stop",
                    usage=TokenUsage(
                        prompt_tokens=120,
                        completion_tokens=20,
                        total_tokens=140,
                        cached_prompt_tokens=90,
                        cache_write_tokens=0,
                    ),
                ),
            ],
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(ctx, gateway=gateway, retrieval=retrieval, backplane=backplane)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="What is the 2024 standard deduction?",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    done = envs[-1]
    assert done["type"] == "done"
    usage = done["data"]["usage"]  # type: ignore[index]
    assert usage == {
        "promptTokens": 220,
        "completionTokens": 30,
        "totalTokens": 250,
        "cachedPromptTokens": 90,
        "cacheWriteTokens": 80,
    }

    # AC-2: one recorded row per answer, tenant-scoped, linked to the message.
    start = envs[0]
    message_id = uuid.UUID(start["data"]["messageId"])  # type: ignore[index]
    async with ctx.sessionmaker() as session:
        rows = await LlmUsageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.message_id == message_id
        assert row.model == "anthropic/claude-opus-4.8"
        assert row.prompt_tokens == 220
        assert row.completion_tokens == 30
        assert row.total_tokens == 250
        assert row.cached_prompt_tokens == 90
        assert row.cache_write_tokens == 80


async def test_usage_row_zeroed_when_provider_omits_usage_and_is_tenant_scoped(
    ctx: _Ctx,
) -> None:
    """#409 AC-3/AC-4 negatives: no provider usage ⇒ zeroed fields but the row still
    lands (never a crash); a foreign tenant's repository sees nothing (INV-1)."""
    import asyncio

    from app.db.repositories import LlmUsageRepository

    gateway = _ScriptedGateway(
        [[StreamEvent(text="An answer."), StreamEvent(finish_reason="stop")]]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(ctx, gateway=gateway, retrieval=_FakeRetrieval([]), backplane=backplane)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="hello",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)
    assert envs[-1]["type"] == "done"

    async with ctx.sessionmaker() as session:
        rows = await LlmUsageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.prompt_tokens == 0
        assert row.completion_tokens == 0
        assert row.total_tokens == 0
        assert row.cached_prompt_tokens == 0
        assert row.cache_write_tokens == 0

        # INV-1: a repository bound to another tenant must see nothing.
        foreign = await LlmUsageRepository(session, uuid.uuid4()).list_for_session(ctx.session_id)
        assert foreign == []
