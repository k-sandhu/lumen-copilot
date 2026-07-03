"""Read-only assistant test/preview/debug harness tests (E6-5, issue #215).

Drives :meth:`app.services.assistant_test_service.AssistantTestService.run_test`
**end-to-end offline**: scripted fake gateways emit the tool-aware stream (a write
tool, a code tool, a search), a permission-aware fake retrieval returns only what a
principal may see, and a SQLite DB captures (or, crucially, does NOT capture) any
side effects. These pin the load-bearing properties of the harness:

* AC-1 (negative): a test run that would call ``write_file`` performs **no real
  write** — the tool is *simulated* (its result says ``simulated``), and NO artifact
  / chat-session / message row is persisted (the internal transcript is rolled back).
  ``run_python`` is *denied* (no sandbox seam wired) — no code run.
* AC-3: the debug trace shows the prompt, the retrieval grounding, each tool call
  with args + result, the outputs, and a duration.
* INV-1/INV-2 (negative): a cross-tenant / non-owned assistant id → 404.
* INV-6: a test run audits ``assistant.tested`` (actor = the caller).

The offline engine is shared: ``execute``'s runtime writes through an injected
non-committing session (the service rolls it back), so a preview leaves the DB clean.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.core.errors import NotFoundError
from app.db import models
from app.db.base import Base
from app.db.repositories import (
    AssistantRepository,
    AuditEventRepository,
    ChatSessionRepository,
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import (
    AutonomyLevel,
    KnowledgeScope,
    Role,
)
from app.domain.llm import StreamEvent, ToolCall
from app.domain.retrieval import DocumentMatch, DocumentText, RetrievedPassage
from app.llm import LLMGateway
from app.services import assistant_test_service
from app.services.assistant_test_service import AssistantTestService
from app.services.audit import AuditSink

# --- Fakes ------------------------------------------------------------------


class _SearchThenAnswerGateway:
    """Searches once, then answers from the retrieved passage (the grounded path)."""

    async def stream_tools(
        self, messages: object, *, tools: object, model: object = None, tool_choice: object = None
    ) -> AsyncIterator[StreamEvent]:
        msgs = list(messages)  # type: ignore[arg-type]
        has_tool_result = any(getattr(m, "role", None).value == "tool" for m in msgs)
        if tool_choice == "none" or has_tool_result:
            yield StreamEvent(text="The 2024 standard deduction is $14,600.")
            yield StreamEvent(finish_reason="stop")
        else:
            yield StreamEvent(
                tool_calls=(ToolCall(id="c1", name="search_text", arguments={"query": "x"}),),
                finish_reason="tool_calls",
            )


class _WriteFileGateway:
    """Calls ``write_file`` once, then answers — the write-tier tool under test."""

    async def stream_tools(
        self, messages: object, *, tools: object, model: object = None, tool_choice: object = None
    ) -> AsyncIterator[StreamEvent]:
        msgs = list(messages)  # type: ignore[arg-type]
        has_tool_result = any(getattr(m, "role", None).value == "tool" for m in msgs)
        if tool_choice == "none" or has_tool_result:
            yield StreamEvent(text="Done — I prepared the report.")
            yield StreamEvent(finish_reason="stop")
        else:
            yield StreamEvent(
                tool_calls=(
                    ToolCall(
                        id="w1",
                        name="write_file",
                        arguments={
                            "filename": "report.md",
                            "content_type": "text/markdown",
                            "content": "# Report\nhello",
                        },
                    ),
                ),
                finish_reason="tool_calls",
            )


class _RunPythonGateway:
    """Calls ``run_python`` once, then answers — the T2 code tool (must be denied)."""

    async def stream_tools(
        self, messages: object, *, tools: object, model: object = None, tool_choice: object = None
    ) -> AsyncIterator[StreamEvent]:
        msgs = list(messages)  # type: ignore[arg-type]
        has_tool_result = any(getattr(m, "role", None).value == "tool" for m in msgs)
        if tool_choice == "none" or has_tool_result:
            yield StreamEvent(text="I could not run code in this preview.")
            yield StreamEvent(finish_reason="stop")
        else:
            yield StreamEvent(
                tool_calls=(
                    ToolCall(id="p1", name="run_python", arguments={"code": "print(1)"}),
                ),
                finish_reason="tool_calls",
            )


class _Retrieval:
    """Returns a fixed passage to any principal (permission modelled elsewhere)."""

    def __init__(self, passage: RetrievedPassage) -> None:
        self._passage = passage

    async def search_text(
        self, *, principal: object, query: str, k: int, collection_ids: object = None
    ) -> list[RetrievedPassage]:
        return [self._passage]

    async def search_documents(
        self, *, principal: object, name_or_query: str, k: int = 10
    ) -> list[DocumentMatch]:
        return []

    async def get_document(self, *, principal: object, document_id: object) -> DocumentText | None:
        return None


# --- Fixture: SQLite engine + seeded tenant/assistant/document --------------


class _Ctx:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        alice: Principal,
        bob: Principal,
        assistant_id: uuid.UUID,
        document_id: uuid.UUID,
        chunk_id: uuid.UUID,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice = alice
        self.bob = bob
        self.assistant_id = assistant_id
        self.document_id = document_id
        self.chunk_id = chunk_id


@pytest_asyncio.fixture
async def ctx(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Ctx]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session.get_sessionmaker", lambda settings=None: factory)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as seed:
            ta = await TenantRepository(seed).create(name="Acme")
            tb = await TenantRepository(seed).create(name="Globex")
            alice = await UserRepository(seed, ta.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            bob = await UserRepository(seed, ta.id).create(
                email="bob@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            coll = await CollectionRepository(seed, ta.id).create(owner_id=alice.id, name="c")
            doc = await DocumentRepository(seed, ta.id).create(
                owner_id=alice.id,
                collection_id=coll.id,
                filename="taxes.pdf",
                mime_type="application/pdf",
                size_bytes=10,
                storage_key=f"{ta.id}/taxes.pdf",
            )
            chunks = await ChunkRepository(seed, ta.id).replace_for_document(
                doc.id,
                [
                    ChunkInput(
                        text="The standard deduction for 2024 is $14,600.",
                        char_start=100,
                        char_end=143,
                    )
                ],
            )
            # A DRAFT assistant owned by alice, whose allow-list offers the write +
            # code tools (so a test run exercises the simulate/deny seam).
            assistant = await AssistantRepository(seed, ta.id).create(
                owner_id=alice.id,
                name="Report builder",
                instructions="You are a tax report builder.",
                knowledge_scope=KnowledgeScope.empty(),
                tool_allowlist=("search_text", "write_file", "run_python"),
                autonomy_level=AutonomyLevel.SUGGEST,
                backup_owner_id=bob.id,
            )
            await seed.commit()
            yield _Ctx(
                sessionmaker=factory,
                tenant_a=ta.id,
                tenant_b=tb.id,
                alice=Principal(user_id=alice.id, tenant_id=ta.id, roles=(Role.MEMBER,)),
                bob=Principal(user_id=bob.id, tenant_id=ta.id, roles=(Role.MEMBER,)),
                assistant_id=assistant.id,
                document_id=doc.id,
                chunk_id=chunks[0].id,
            )
    finally:
        await engine.dispose()


def _passage(ctx: _Ctx) -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=ctx.chunk_id,
        document_id=ctx.document_id,
        document_name="taxes.pdf",
        ord=0,
        text="The standard deduction for 2024 is $14,600.",
        char_start=100,
        char_end=143,
        score=0.9,
    )


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, *, gateway: object, retrieval: object) -> None:
    """Wire the fake gateway + retrieval into the runtime the test service builds."""
    real_cls = assistant_test_service.ChatRuntime

    def _factory(**kwargs: object) -> object:
        kwargs["gateway"] = gateway
        kwargs["retrieval_factory"] = lambda _session: retrieval
        return real_cls(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(assistant_test_service, "ChatRuntime", _factory)


async def _service(
    ctx: _Ctx, session: AsyncSession, *, principal: Principal
) -> AssistantTestService:
    return AssistantTestService(
        session,
        principal=principal,
        gateway=LLMGateway.__new__(LLMGateway),  # never called (runtime is patched)
        audit=AuditSink(AuditEventRepository(session, principal.tenant_id)),
        request_id="test-req",
        source_ip="127.0.0.1",
        runtime_sessionmaker=ctx.sessionmaker,
    )


async def _count(session: AsyncSession, model: type, tenant_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
    return int((await session.execute(stmt)).scalar_one())


# --- AC-1 (negative): a test run performs NO real write ---------------------


async def test_write_file_is_simulated_no_artifact_or_transcript(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: a write tool is *simulated*, and NO artifact / chat session / message persists."""
    _patch_runtime(monkeypatch, gateway=_WriteFileGateway(), retrieval=_Retrieval(_passage(ctx)))

    async with ctx.sessionmaker() as session:
        service = await _service(ctx, session, principal=ctx.alice)
        trace = await service.run_test(ctx.assistant_id, input_text="build the report")
        await session.commit()

    # The write_file call is in the trace and its result is a SIMULATION, not a write.
    write_calls = [c for c in trace.tool_calls if c["tool"] == "write_file"]
    assert len(write_calls) == 1
    result = write_calls[0]["result"]
    assert result is not None and result["ok"] is True
    assert "simulated" in str(result["summary"]).lower()

    # No real side effect: no artifact row, no chat session, no message persisted.
    async with ctx.sessionmaker() as session:
        assert await _count(session, models.Artifact, ctx.tenant_a) == 0
        assert await _count(session, models.ChatSession, ctx.tenant_a) == 0
        assert await _count(session, models.Message, ctx.tenant_a) == 0
        # Belt-and-braces: the repositories agree (nothing under alice/session).
        sessions = await ChatSessionRepository(session, ctx.tenant_a).list_for_owner(
            ctx.alice.user_id
        )
        assert sessions == []


async def test_run_python_is_denied_no_code_run(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1/AC-N: the T2 code tool is DENIED in a preview (no sandbox seam) — no code run."""
    _patch_runtime(monkeypatch, gateway=_RunPythonGateway(), retrieval=_Retrieval(_passage(ctx)))

    async with ctx.sessionmaker() as session:
        service = await _service(ctx, session, principal=ctx.alice)
        trace = await service.run_test(ctx.assistant_id, input_text="crunch numbers")
        await session.commit()

    py_calls = [c for c in trace.tool_calls if c["tool"] == "run_python"]
    assert len(py_calls) == 1
    result = py_calls[0]["result"]
    # Denied (ok=False) — the sandbox seam is not wired for a test run, so no
    # container launched and no code_runs row exists.
    assert result is not None and result["ok"] is False
    async with ctx.sessionmaker() as session:
        assert await _count(session, models.CodeRun, ctx.tenant_a) == 0


# --- AC-3: the debug trace shows prompt + retrieval + tool calls + timing ----


async def test_debug_trace_has_prompt_retrieval_tools_outputs_timing(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: the trace carries the effective prompt, retrieval, tool calls, outputs, timing."""
    _patch_runtime(
        monkeypatch, gateway=_SearchThenAnswerGateway(), retrieval=_Retrieval(_passage(ctx))
    )

    async with ctx.sessionmaker() as session:
        service = await _service(ctx, session, principal=ctx.alice)
        trace = await service.run_test(ctx.assistant_id, input_text="what is the deduction?")
        await session.commit()

    # Prompt is the persona-augmented system prompt (instructions prepend grounding).
    assert "tax report builder" in trace.prompt
    assert trace.input == "what is the deduction?"
    assert trace.model  # a resolved model id
    # Retrieval grounding: exactly the permitted passage the tool returned (INV-3).
    assert len(trace.retrieval) == 1
    assert trace.retrieval[0]["documentName"] == "taxes.pdf"
    # A tool call with args + result is in the trace.
    search_calls = [c for c in trace.tool_calls if c["tool"] == "search_text"]
    assert len(search_calls) == 1
    assert search_calls[0]["args"] == {"query": "x"}
    assert search_calls[0]["result"] is not None
    # Outputs + timing + success.
    assert "14,600" in trace.outputs
    assert trace.succeeded is True
    assert trace.errors == []
    assert trace.duration_ms >= 0


# --- INV-1/INV-2 (negative): cross-tenant / non-owned assistant → 404 --------


async def test_cross_tenant_assistant_is_404(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INV-1: an assistant in tenant A is invisible to a caller in tenant B (404)."""
    _patch_runtime(
        monkeypatch, gateway=_SearchThenAnswerGateway(), retrieval=_Retrieval(_passage(ctx))
    )
    stranger = Principal(user_id=uuid.uuid4(), tenant_id=ctx.tenant_b, roles=(Role.MEMBER,))
    async with ctx.sessionmaker() as session:
        service = await _service(ctx, session, principal=stranger)
        with pytest.raises(NotFoundError):
            await service.run_test(ctx.assistant_id, input_text="hi")


async def test_non_owner_assistant_is_404(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INV-2: a non-owner (same tenant, not admin) cannot test another user's draft (404)."""
    _patch_runtime(
        monkeypatch, gateway=_SearchThenAnswerGateway(), retrieval=_Retrieval(_passage(ctx))
    )
    async with ctx.sessionmaker() as session:
        service = await _service(ctx, session, principal=ctx.bob)  # bob does not own it
        with pytest.raises(NotFoundError):
            await service.run_test(ctx.assistant_id, input_text="hi")


async def test_admin_may_test_any_assistant_in_tenant(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tenant admin may preview any assistant (the owner-or-admin rule, matching CRUD)."""
    _patch_runtime(
        monkeypatch, gateway=_SearchThenAnswerGateway(), retrieval=_Retrieval(_passage(ctx))
    )
    admin = Principal(user_id=uuid.uuid4(), tenant_id=ctx.tenant_a, roles=(Role.ADMIN,))
    async with ctx.sessionmaker() as session:
        service = await _service(ctx, session, principal=admin)
        trace = await service.run_test(ctx.assistant_id, input_text="ok")
        await session.commit()
    assert trace.succeeded is True


# --- INV-6: a test run is audited ------------------------------------------


async def test_test_run_is_audited(ctx: _Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-6: a test run emits assistant.tested, actor = the caller."""
    _patch_runtime(
        monkeypatch, gateway=_SearchThenAnswerGateway(), retrieval=_Retrieval(_passage(ctx))
    )
    async with ctx.sessionmaker() as session:
        service = await _service(ctx, session, principal=ctx.alice)
        await service.run_test(ctx.assistant_id, input_text="hi")
        await session.commit()

    async with ctx.sessionmaker() as session:
        recent = await AuditEventRepository(session, ctx.tenant_a).list_recent(limit=100)
    tested = [e for e in recent if e.action == "assistant.tested"]
    assert len(tested) == 1
    assert tested[0].resource_id == str(ctx.assistant_id)
    assert tested[0].actor_id == ctx.alice.user_id
