"""Headless agent-run service tests — the E5 backbone (ADR-0015, issue #235).

Drives :func:`app.services.runs_service.execute_run` **end-to-end offline**: a
scripted fake gateway emits the tool-aware stream, a permission-aware fake
retrieval returns only what a given principal may see, and a SQLite DB captures the
run record + transcript + citations + audit. The real agentic loop (a live model
deciding to search) needs OPENROUTER_API_KEY + the stack; these fix the *headless
plumbing*: a run with **no WS client** persists a cited transcript + terminal status
(AC-1), a run cannot retrieve what its runner lacks (INV-2, the load-bearing
property), a cross-tenant run is 404 (INV-1), a crash → ``failed`` not stuck
``running`` (AC-4), and every run is bracketed by ``run.started``/``run.finished``
(INV-6).

The engine is monkeypatched so ``tenant_session_scope`` (used by ``execute_run``)
and the runtime's own sessionmaker both bind to the one offline SQLite engine.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.repositories import (
    AssistantRepository,
    AssistantVersionRepository,
    AuditEventRepository,
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    RunRepository,
    RunStepRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import (
    AssistantStatus,
    AutonomyLevel,
    KnowledgeScope,
    Role,
    RunStatus,
    RunStepKind,
    RunTrigger,
)
from app.domain.llm import StreamEvent, ToolCall
from app.domain.retrieval import DocumentMatch, DocumentText, RetrievedPassage
from app.services import runs_service
from app.services.assistants_service import config_from_assistant

import app.db.models  # noqa: F401  isort: skip


# --- Fakes ------------------------------------------------------------------


class _ScriptedGateway:
    """A gateway that searches once, then answers from the retrieved passage."""

    async def stream_tools(
        self,
        messages: object,
        *,
        tools: object,
        model: object = None,
        tool_choice: object = None,
        api_key: object = None,
        api_base: object = None,
        cache_key: object = None,
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
        cache_key: object = None,
    ) -> AsyncIterator[StreamEvent]:
        raise RuntimeError("provider exploded")
        yield  # pragma: no cover — makes this an async generator


class _AppErrorGateway:
    """A gateway that fails the run with a typed :class:`AppError` (escalation codes).

    The runtime maps a raised :class:`AppError` to a terminal ``error`` envelope whose
    ``code`` is the app error's code — e.g. ``ForbiddenError`` → ``forbidden``
    (restricted data, escalation-worthy), ``DependencyError`` → ``dependency_unavailable``
    (transient). This lets a run's terminal reason be scripted offline.
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def stream_tools(
        self,
        messages: object,
        *,
        tools: object,
        model: object = None,
        tool_choice: object = None,
        api_key: object = None,
        api_base: object = None,
        cache_key: object = None,
    ) -> AsyncIterator[StreamEvent]:
        raise self._exc
        yield  # pragma: no cover — makes this an async generator


class _PermissionedRetrieval:
    """Retrieval that returns a passage **only** to principals in ``allowed_users``.

    Models the INV-2 permission predicate inside ``retrieval/``: the same query run
    as a user without the grant returns zero passages (never the content). Keyed off
    the principal the runtime passes — which is the run *owner* — so a headless run
    inherits exactly the owner's grants.
    """

    def __init__(self, passage: RetrievedPassage, allowed_users: set[uuid.UUID]) -> None:
        self._passage = passage
        self._allowed = allowed_users

    async def search_text(
        self,
        *,
        principal: object,
        query: str,
        k: int,
        collection_ids: object = None,
        document_ids: object = None,
    ) -> list[RetrievedPassage]:
        user_id = getattr(principal, "user_id", None)
        return [self._passage] if user_id in self._allowed else []

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
        alice_id: uuid.UUID,
        bob_id: uuid.UUID,
        assistant_id: uuid.UUID,
        version_id: uuid.UUID,
        document_id: uuid.UUID,
        chunk_id: uuid.UUID,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice_id = alice_id
        self.bob_id = bob_id
        self.assistant_id = assistant_id
        self.version_id = version_id
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
    # ``execute_run`` opens its own sessions via ``tenant_session_scope`` +
    # ``get_sessionmaker`` — point both at this one offline engine.
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
                acl_enforced=False,
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
            # A published assistant owned by alice, pinned to one version.
            assistants = AssistantRepository(seed, ta.id)
            assistant = await assistants.create(
                owner_id=alice.id,
                name="Weekly summary",
                instructions="You are a tax summarizer.",
                knowledge_scope=KnowledgeScope.empty(),
                tool_allowlist=(),
                autonomy_level=AutonomyLevel.SUGGEST,
                backup_owner_id=bob.id,
            )
            await assistants.update(assistant.id, fields={"status": AssistantStatus.PUBLISHED})
            published = await assistants.get(assistant.id)
            version = await AssistantVersionRepository(seed, ta.id).add(
                assistant_id=assistant.id,
                version=1,
                author_id=alice.id,
                config=config_from_assistant(published),
            )
            await seed.commit()
            yield _Ctx(
                sessionmaker=factory,
                tenant_a=ta.id,
                tenant_b=tb.id,
                alice_id=alice.id,
                bob_id=bob.id,
                assistant_id=assistant.id,
                version_id=version.id,
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
    """Wire the fake gateway + retrieval into the runtime the run service builds."""
    real_cls = runs_service.ChatRuntime

    def _factory(**kwargs: object) -> object:
        kwargs["gateway"] = gateway
        kwargs["retrieval_factory"] = lambda _session: retrieval
        return real_cls(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runs_service, "ChatRuntime", _factory)


def _wire_alice_grant(ctx: _Ctx, monkeypatch: pytest.MonkeyPatch, *, gateway: object) -> None:
    """The common wiring: a permissioned retrieval where only alice may see the passage."""
    _patch_runtime(
        monkeypatch,
        gateway=gateway,
        retrieval=_PermissionedRetrieval(_passage(ctx), {ctx.alice_id}),
    )


async def _create_queued_run(
    ctx: _Ctx, *, owner_id: uuid.UUID, inputs: dict[str, object] | None = None
) -> uuid.UUID:
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).create(
            owner_id=owner_id,
            assistant_id=ctx.assistant_id,
            assistant_version_id=ctx.version_id,
            trigger=RunTrigger.MANUAL,
            inputs=inputs,
        )
        await session.commit()
        return run.id


# --- AC-1: a run executes headless and persists a cited transcript ----------


async def test_run_executes_headless_with_no_ws_client(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: a run runs end-to-end with no socket and persists a cited transcript + terminal."""
    _wire_alice_grant(ctx, monkeypatch, gateway=_ScriptedGateway())
    run_id = await _create_queued_run(ctx, owner_id=ctx.alice_id)

    status = await runs_service.execute_run(run_id, ctx.tenant_a)

    assert status is RunStatus.SUCCEEDED
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None
        assert run.status is RunStatus.SUCCEEDED
        assert run.started_at is not None and run.finished_at is not None
        assert run.error is None
        assert run.summary and "14,600" in run.summary
        assert run.message_id is not None
        # The transcript persisted the tool + citation + delta steps (no socket).
        steps = await RunStepRepository(session, ctx.tenant_a).list_for_run(run_id)
        kinds = {s.kind for s in steps}
        assert RunStepKind.TOOL_CALL in kinds
        assert RunStepKind.TOOL_RESULT in kinds
        assert RunStepKind.CITATION in kinds
        assert RunStepKind.DELTA in kinds
        # seq is monotonic + unique (the WS ordering guarantee).
        seqs = [s.seq for s in steps]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


async def test_run_detail_hydrates_grounded_citations(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read service returns the run's grounded citations (INV-3, shared chain)."""
    _wire_alice_grant(ctx, monkeypatch, gateway=_ScriptedGateway())
    run_id = await _create_queued_run(ctx, owner_id=ctx.alice_id)
    await runs_service.execute_run(run_id, ctx.tenant_a)

    async with ctx.sessionmaker() as session:
        detail = await runs_service.RunsReadService(
            session, tenant_id=ctx.tenant_a, owner_id=ctx.alice_id
        ).get(run_id)
        assert detail.run.status is RunStatus.SUCCEEDED
        assert len(detail.citations) == 1
        cite = detail.citations[0]
        assert cite.document_name == "taxes.pdf"
        assert "14,600" in cite.snippet
        assert len(detail.steps) >= 4


# --- INV-2: a headless run cannot retrieve what its runner lacks ------------


async def test_headless_run_cannot_retrieve_what_runner_lacks(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INV-2 (load-bearing): a run whose owner lacks the grant gets ZERO passages.

    Same assistant, same query, same document — only the run *owner* changes. Alice
    is allowed the passage; bob is not. Bob's run must produce zero citations (the
    permission filter runs as the run's owner, so automation is not an ambient-
    authority backdoor around the permission model).
    """
    retrieval = _PermissionedRetrieval(_passage(ctx), {ctx.alice_id})  # only alice may see it
    _patch_runtime(monkeypatch, gateway=_ScriptedGateway(), retrieval=retrieval)

    # Bob owns this run — he lacks the grant.
    bob_run = await _create_queued_run(ctx, owner_id=ctx.bob_id)
    await runs_service.execute_run(bob_run, ctx.tenant_a)

    async with ctx.sessionmaker() as session:
        detail = await runs_service.RunsReadService(
            session, tenant_id=ctx.tenant_a, owner_id=ctx.bob_id
        ).get(bob_run)
        # Zero citations — bob's run could not read alice's document.
        assert detail.citations == []
        citation_steps = [s for s in detail.steps if s.kind is RunStepKind.CITATION]
        assert citation_steps == []

    # And alice's run over the exact same setup DOES see it (the contrast proves the
    # filter is the owner's, not a blanket denial).
    alice_run = await _create_queued_run(ctx, owner_id=ctx.alice_id)
    await runs_service.execute_run(alice_run, ctx.tenant_a)
    async with ctx.sessionmaker() as session:
        detail = await runs_service.RunsReadService(
            session, tenant_id=ctx.tenant_a, owner_id=ctx.alice_id
        ).get(alice_run)
        assert len(detail.citations) == 1


# --- INV-1: cross-tenant run id → 404 --------------------------------------


async def test_cross_tenant_run_is_not_found(ctx: _Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-1: a run in tenant A is invisible to a reader scoped to tenant B (404)."""
    from app.core.errors import NotFoundError

    _wire_alice_grant(ctx, monkeypatch, gateway=_ScriptedGateway())
    run_id = await _create_queued_run(ctx, owner_id=ctx.alice_id)
    await runs_service.execute_run(run_id, ctx.tenant_a)

    async with ctx.sessionmaker() as session:
        # A reader in tenant B (Globex) cannot see Acme's run — repository is
        # tenant-scoped, so the row does not resolve → NotFoundError (→ 404).
        reader = runs_service.RunsReadService(
            session, tenant_id=ctx.tenant_b, owner_id=ctx.alice_id
        )
        with pytest.raises(NotFoundError):
            await reader.get(run_id)


async def test_non_owner_run_is_not_found(ctx: _Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-2: a run owned by another user in the same tenant is 404 (existence non-disclosure)."""
    from app.core.errors import NotFoundError

    _wire_alice_grant(ctx, monkeypatch, gateway=_ScriptedGateway())
    run_id = await _create_queued_run(ctx, owner_id=ctx.alice_id)
    await runs_service.execute_run(run_id, ctx.tenant_a)

    async with ctx.sessionmaker() as session:
        # Bob (same tenant, not the owner) cannot see alice's run.
        with pytest.raises(NotFoundError):
            await runs_service.RunsReadService(
                session, tenant_id=ctx.tenant_a, owner_id=ctx.bob_id
            ).get(run_id)


# --- AC-4: a crash → failed status, never a stuck running -------------------


async def test_crash_sets_failed_never_stuck_running(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4/INV-8: a runtime that blows up leaves the run ``failed`` with a typed error."""
    _wire_alice_grant(ctx, monkeypatch, gateway=_BoomGateway())
    run_id = await _create_queued_run(ctx, owner_id=ctx.alice_id)

    status = await runs_service.execute_run(run_id, ctx.tenant_a)

    assert status is RunStatus.FAILED
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None
        assert run.status is RunStatus.FAILED  # never stuck ``running``
        assert run.finished_at is not None
        assert run.error is not None  # a typed reason, never a raw vendor string
        assert run.error.code and run.error.message


async def test_missing_version_fails_reproducibly(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose pinned version is gone fails (a run must run against a frozen version)."""
    _wire_alice_grant(ctx, monkeypatch, gateway=_ScriptedGateway())
    # Create a run pinned to a non-existent version id.
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).create(
            owner_id=ctx.alice_id,
            assistant_id=ctx.assistant_id,
            assistant_version_id=uuid.uuid4(),
            trigger=RunTrigger.MANUAL,
        )
        await session.commit()
        run_id = run.id

    status = await runs_service.execute_run(run_id, ctx.tenant_a)
    assert status is RunStatus.FAILED
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None and run.status is RunStatus.FAILED
        assert run.error is not None and run.error.code == "version_missing"


# --- INV-6: run start/finish audited ---------------------------------------


async def test_run_start_and_finish_audited(ctx: _Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-6: a run emits run.started + run.finished, actor = the run owner."""
    _wire_alice_grant(ctx, monkeypatch, gateway=_ScriptedGateway())
    run_id = await _create_queued_run(ctx, owner_id=ctx.alice_id)
    await runs_service.execute_run(run_id, ctx.tenant_a)

    async with ctx.sessionmaker() as session:
        recent = await AuditEventRepository(session, ctx.tenant_a).list_recent(limit=100)
    events = [e for e in recent if e.resource_id == str(run_id)]
    actions = {e.action for e in events}
    assert "run.started" in actions
    assert "run.finished" in actions
    # Actor is the owner (the run acts on their behalf), not "system".
    for e in events:
        if e.action in {"run.started", "run.finished"}:
            assert e.actor_id == ctx.alice_id


# --- Duplicate delivery: an already-claimed run is not re-run ---------------


async def test_already_terminal_run_is_not_rerun(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second delivery of a run that already ran is a no-op (idempotent claim)."""
    _wire_alice_grant(ctx, monkeypatch, gateway=_ScriptedGateway())
    run_id = await _create_queued_run(ctx, owner_id=ctx.alice_id)
    first = await runs_service.execute_run(run_id, ctx.tenant_a)
    assert first is RunStatus.SUCCEEDED
    # Re-deliver: the run is no longer ``queued``, so it is not re-run.
    second = await runs_service.execute_run(run_id, ctx.tenant_a)
    assert second is RunStatus.SUCCEEDED
    async with ctx.sessionmaker() as session:
        steps = await RunStepRepository(session, ctx.tenant_a).list_for_run(run_id)
        # The transcript was written exactly once (not duplicated by the second run).
        citation_steps = [s for s in steps if s.kind is RunStepKind.CITATION]
        assert len(citation_steps) == 1


# --- enqueue_manual_run (the run-now / manual trigger entry point) ----------


async def test_enqueue_manual_run_creates_queued_pinned_run(ctx: _Ctx) -> None:
    """The manual-trigger path creates a ``queued`` run pinned to the published head."""
    async with ctx.sessionmaker() as session:
        run = await runs_service.enqueue_manual_run(
            session,
            tenant_id=ctx.tenant_a,
            owner_id=ctx.alice_id,
            assistant_id=ctx.assistant_id,
            inputs={"prompt": "go"},
        )
        await session.commit()
        assert run.status is RunStatus.QUEUED
        assert run.trigger is RunTrigger.MANUAL
        assert run.assistant_version_id == ctx.version_id  # pinned to the head version
        assert run.inputs == {"prompt": "go"}
        assert run.owner_id == ctx.alice_id


async def test_enqueue_manual_run_unknown_assistant_is_404(ctx: _Ctx) -> None:
    from app.core.errors import NotFoundError

    async with ctx.sessionmaker() as session:
        with pytest.raises(NotFoundError):
            await runs_service.enqueue_manual_run(
                session,
                tenant_id=ctx.tenant_a,
                owner_id=ctx.alice_id,
                assistant_id=uuid.uuid4(),
            )


async def test_enqueue_manual_run_non_owner_is_404(ctx: _Ctx) -> None:
    """INV-2: a non-owner cannot run another user's assistant (404, existence non-disclosure)."""
    from app.core.errors import NotFoundError

    async with ctx.sessionmaker() as session:
        with pytest.raises(NotFoundError):
            await runs_service.enqueue_manual_run(
                session,
                tenant_id=ctx.tenant_a,
                owner_id=ctx.bob_id,  # bob does not own alice's assistant
                assistant_id=ctx.assistant_id,
            )


async def test_enqueue_manual_run_unpublished_assistant_is_422(ctx: _Ctx) -> None:
    """INV-8: an unpublished assistant cannot be run (a run needs a frozen version)."""
    from app.core.errors import ValidationError

    # Create a fresh draft (never published) owned by alice.
    async with ctx.sessionmaker() as session:
        draft = await AssistantRepository(session, ctx.tenant_a).create(
            owner_id=ctx.alice_id,
            name="Draft",
            knowledge_scope=KnowledgeScope.empty(),
            tool_allowlist=(),
            autonomy_level=AutonomyLevel.SUGGEST,
        )
        await session.commit()
        draft_id = draft.id

    async with ctx.sessionmaker() as session:
        with pytest.raises(ValidationError):
            await runs_service.enqueue_manual_run(
                session,
                tenant_id=ctx.tenant_a,
                owner_id=ctx.alice_id,
                assistant_id=draft_id,
            )


# --- E7-5 (#239): escalation on a human-decidable failure -------------------


async def test_restricted_data_failure_escalates_not_failed(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: a run that fails for a human-decidable reason ends ``escalated``, not silent.

    A ``ForbiddenError`` (restricted data) surfaces as ``error.code == "forbidden"``,
    which the classifier routes to ``escalated`` — a human should decide, so the run
    is NOT silently dropped and NOT a plain ``failed``.
    """
    from app.core.errors import ForbiddenError

    _patch_runtime(
        monkeypatch,
        gateway=_AppErrorGateway(ForbiddenError("You cannot read this.")),
        retrieval=_PermissionedRetrieval(_passage(ctx), {ctx.alice_id}),
    )
    run_id = await _create_queued_run(ctx, owner_id=ctx.alice_id)

    status = await runs_service.execute_run(run_id, ctx.tenant_a)

    assert status is RunStatus.ESCALATED
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None
        assert run.status is RunStatus.ESCALATED  # a human must handle it
        assert run.finished_at is not None  # a real terminal, never stuck
        assert run.error is not None and run.error.code == "forbidden"


async def test_escalation_is_audited(ctx: _Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-2/INV-6: an escalation emits ``run.escalated`` with the blocking reason."""
    from app.core.errors import ForbiddenError

    _patch_runtime(
        monkeypatch,
        gateway=_AppErrorGateway(ForbiddenError("nope")),
        retrieval=_PermissionedRetrieval(_passage(ctx), {ctx.alice_id}),
    )
    run_id = await _create_queued_run(ctx, owner_id=ctx.alice_id)
    await runs_service.execute_run(run_id, ctx.tenant_a)

    async with ctx.sessionmaker() as session:
        recent = await AuditEventRepository(session, ctx.tenant_a).list_recent(limit=100)
    events = [e for e in recent if e.resource_id == str(run_id)]
    actions = {e.action for e in events}
    assert "run.escalated" in actions  # the owner is notified via the audited status
    escalated = next(e for e in events if e.action == "run.escalated")
    assert escalated.metadata.get("reason") == "restricted_data"
    assert escalated.actor_id == ctx.alice_id  # the run's owner


async def test_internal_failure_stays_failed_not_escalated(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deny-by-default: a generic internal defect stays ``failed`` (not a human's job)."""
    _wire_alice_grant(ctx, monkeypatch, gateway=_BoomGateway())  # → internal_error
    run_id = await _create_queued_run(ctx, owner_id=ctx.alice_id)

    status = await runs_service.execute_run(run_id, ctx.tenant_a)

    assert status is RunStatus.FAILED
    async with ctx.sessionmaker() as session:
        recent = await AuditEventRepository(session, ctx.tenant_a).list_recent(limit=100)
    actions = {e.action for e in recent if e.resource_id == str(run_id)}
    assert "run.escalated" not in actions


# --- AC-3: transient faults retry with backoff before a terminal ------------


async def test_transient_fault_retries_before_terminal(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: a transient dependency fault (budget left) re-queues + signals a retry.

    ``execute_run`` raises :class:`TransientRunRetry` (the Celery wrapper turns that
    into ``self.retry``) and leaves the run ``queued`` — never a terminal on the first
    transient blip. This is the "retry before escalation, never silently drop" path.
    """
    from app.core.errors import DependencyError

    _patch_runtime(
        monkeypatch,
        gateway=_AppErrorGateway(DependencyError("model briefly down")),
        retrieval=_PermissionedRetrieval(_passage(ctx), {ctx.alice_id}),
    )
    run_id = await _create_queued_run(ctx, owner_id=ctx.alice_id)

    with pytest.raises(runs_service.TransientRunRetry) as excinfo:
        await runs_service.execute_run(run_id, ctx.tenant_a, attempt=0)
    assert excinfo.value.backoff_seconds > 0

    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None
        # Reset to queued for the re-drive — NOT a terminal, NOT stuck running.
        assert run.status is RunStatus.QUEUED
        assert run.finished_at is None
        assert run.error is None


async def test_transient_fault_terminal_after_retries_exhausted(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: once the retry budget is exhausted a transient fault reaches ``failed``.

    A run is never dropped: on the final attempt the fold writes a queryable terminal
    (a transient reason is NOT escalation-worthy, so it stays ``failed`` — a human is
    not paged for a dependency blip).
    """
    from app.core.errors import DependencyError

    _patch_runtime(
        monkeypatch,
        gateway=_AppErrorGateway(DependencyError("still down")),
        retrieval=_PermissionedRetrieval(_passage(ctx), {ctx.alice_id}),
    )
    run_id = await _create_queued_run(ctx, owner_id=ctx.alice_id)

    # attempt == run_max_retries ⇒ budget exhausted ⇒ terminal, no retry raised.
    from app.core.config import get_settings

    exhausted = get_settings().run_max_retries
    status = await runs_service.execute_run(run_id, ctx.tenant_a, attempt=exhausted)

    assert status is RunStatus.FAILED
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None
        assert run.status is RunStatus.FAILED
        assert run.error is not None and run.error.code == "dependency_unavailable"


# --- Resume / cancel / reroute (the human handoff) --------------------------


async def _escalate_run(ctx: _Ctx, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """Drive a run to the ``escalated`` terminal (a restricted-data stop) and return it."""
    from app.core.errors import ForbiddenError

    _patch_runtime(
        monkeypatch,
        gateway=_AppErrorGateway(ForbiddenError("nope")),
        retrieval=_PermissionedRetrieval(_passage(ctx), {ctx.alice_id}),
    )
    run_id = await _create_queued_run(ctx, owner_id=ctx.alice_id)
    status = await runs_service.execute_run(run_id, ctx.tenant_a)
    assert status is RunStatus.ESCALATED
    return run_id


def _control_service(
    ctx: _Ctx, session: AsyncSession, *, owner_id: uuid.UUID
) -> runs_service.RunsControlService:
    from app.db.repositories import AuditEventRepository
    from app.services.audit import AuditSink

    return runs_service.RunsControlService(
        session,
        tenant_id=ctx.tenant_a,
        owner_id=owner_id,
        audit=AuditSink(AuditEventRepository(session, ctx.tenant_a)),
        request_id="test-req",
        source_ip="203.0.113.1",
    )


async def test_owner_resume_requeues_escalated_run(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: the owner resumes an escalated run — it goes back to ``queued`` + is audited."""
    run_id = await _escalate_run(ctx, monkeypatch)
    async with ctx.sessionmaker() as session:
        result = await _control_service(ctx, session, owner_id=ctx.alice_id).resume(run_id)
        await session.commit()
        assert result.requeued is True
        assert result.run.status is RunStatus.QUEUED
        assert result.run.error is None  # the escalation reason is cleared
    async with ctx.sessionmaker() as session:
        recent = await AuditEventRepository(session, ctx.tenant_a).list_recent(limit=100)
    actions = {e.action for e in recent if e.resource_id == str(run_id)}
    assert "run.resumed" in actions


async def test_owner_cancel_closes_escalated_run(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: cancel closes an escalated run to a permanent terminal (never re-queued)."""
    run_id = await _escalate_run(ctx, monkeypatch)
    async with ctx.sessionmaker() as session:
        result = await _control_service(ctx, session, owner_id=ctx.alice_id).cancel(run_id)
        await session.commit()
        assert result.requeued is False
        assert result.run.status is RunStatus.FAILED
        assert result.run.error is not None and result.run.error.code == "cancelled"
    async with ctx.sessionmaker() as session:
        recent = await AuditEventRepository(session, ctx.tenant_a).list_recent(limit=100)
    actions = {e.action for e in recent if e.resource_id == str(run_id)}
    assert "run.cancelled" in actions


async def test_owner_reroute_reassigns_and_requeues(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: reroute reassigns the run to another owner (its new principal) + re-queues."""
    run_id = await _escalate_run(ctx, monkeypatch)
    async with ctx.sessionmaker() as session:
        result = await _control_service(ctx, session, owner_id=ctx.alice_id).reroute(
            run_id, to_owner_id=ctx.bob_id
        )
        await session.commit()
        assert result.requeued is True
        assert result.run.status is RunStatus.QUEUED
        assert result.run.owner_id == ctx.bob_id  # bob is the new execution principal
    async with ctx.sessionmaker() as session:
        recent = await AuditEventRepository(session, ctx.tenant_a).list_recent(limit=100)
    rerouted = next(
        e for e in recent if e.resource_id == str(run_id) and e.action == "run.rerouted"
    )
    assert rerouted.metadata.get("to_owner_id") == str(ctx.bob_id)


# --- Negatives: not-escalated → 409, non-owner/cross-tenant → 404 -----------


async def test_resume_non_escalated_run_is_409(ctx: _Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-8: resuming a run that is not escalated is an illegal transition → 409."""
    from app.core.errors import ConflictError

    _wire_alice_grant(ctx, monkeypatch, gateway=_ScriptedGateway())
    run_id = await _create_queued_run(ctx, owner_id=ctx.alice_id)
    await runs_service.execute_run(run_id, ctx.tenant_a)  # → succeeded, not escalated
    async with ctx.sessionmaker() as session:
        with pytest.raises(ConflictError):
            await _control_service(ctx, session, owner_id=ctx.alice_id).resume(run_id)


async def test_non_owner_cannot_resume_escalated_run_404(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INV-2: a non-owner cannot act on another user's escalated run (404, non-disclosure)."""
    from app.core.errors import NotFoundError

    run_id = await _escalate_run(ctx, monkeypatch)  # alice owns it
    async with ctx.sessionmaker() as session:
        with pytest.raises(NotFoundError):
            await _control_service(ctx, session, owner_id=ctx.bob_id).resume(run_id)
        with pytest.raises(NotFoundError):
            await _control_service(ctx, session, owner_id=ctx.bob_id).cancel(run_id)


async def test_cross_tenant_cannot_resume_escalated_run_404(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INV-1: a reader scoped to another tenant cannot resume the run (404)."""
    from app.core.errors import NotFoundError
    from app.services.audit import AuditSink

    run_id = await _escalate_run(ctx, monkeypatch)
    async with ctx.sessionmaker() as session:
        control = runs_service.RunsControlService(
            session,
            tenant_id=ctx.tenant_b,  # a different tenant
            owner_id=ctx.alice_id,
            audit=AuditSink(AuditEventRepository(session, ctx.tenant_b)),
            request_id="r",
            source_ip="203.0.113.1",
        )
        with pytest.raises(NotFoundError):
            await control.resume(run_id)


async def test_reroute_to_current_owner_is_422(ctx: _Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-8: a no-op reroute (to the current owner) is malformed → 422."""
    from app.core.errors import ValidationError

    run_id = await _escalate_run(ctx, monkeypatch)
    async with ctx.sessionmaker() as session:
        with pytest.raises(ValidationError):
            await _control_service(ctx, session, owner_id=ctx.alice_id).reroute(
                run_id, to_owner_id=ctx.alice_id
            )


async def test_reroute_to_unknown_target_is_404(ctx: _Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-1: rerouting to a user not in the tenant is 404 (existence non-disclosure)."""
    from app.core.errors import NotFoundError

    run_id = await _escalate_run(ctx, monkeypatch)
    async with ctx.sessionmaker() as session:
        with pytest.raises(NotFoundError):
            await _control_service(ctx, session, owner_id=ctx.alice_id).reroute(
                run_id, to_owner_id=uuid.uuid4()
            )
