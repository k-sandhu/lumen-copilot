"""Rolling session summary + evidence carry-forward (#416, ADR-0016 §3.2).

Offline coverage for the storage + the async summarizer:

* the one-row-per-session repository: evidence digest round-trip (IDs only),
  summary upsert with version increments, FORWARD-ONLY coverage (a stale/racing
  task never regresses the boundary), malformed stored evidence tolerated;
* the Celery task body: skips below the batch threshold, preserves the verbatim
  tail, rolls the right turns into the summary via the (faked) gateway, emits
  the ``session.summarized`` audit + the summarizer's own ``llm_usage`` row,
  and CONTAINS every failure (AC-4 — a dead gateway yields a logged skip,
  never a raised error).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import session as db_session
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    ChatSessionRepository,
    LlmUsageRepository,
    MessageRepository,
    SessionSummaryRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import MessageRole, Role
from app.domain.llm import ChatMessage, Completion, TokenUsage

import app.db.models  # noqa: F401  isort: skip


class _Ctx:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        message_ids: list[uuid.UUID],
    ) -> None:
        self.sessionmaker = sessionmaker
        self.tenant_id = tenant_id
        self.session_id = session_id
        self.message_ids = message_ids


@pytest_asyncio.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    prev_maker = db_session._sessionmaker  # noqa: SLF001 — the task-test seam
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        # The summarize task opens sessions via the module-global maker
        # (``tenant_session_scope``); point it at this test's SQLite.
        db_session._sessionmaker = factory  # noqa: SLF001
        async with factory() as seed:
            tenant = await TenantRepository(seed).create(name="Acme")
            user = await UserRepository(seed, tenant.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            chat = await ChatSessionRepository(seed, tenant.id).create(
                owner_id=user.id, model="m", title="t"
            )
            messages = MessageRepository(seed, tenant.id)
            ids: list[uuid.UUID] = []
            for i in range(14):
                role = MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT
                m = await messages.add(
                    session_id=chat.id, role=role, content=f"turn {i}: the sky is {i}"
                )
                ids.append(m.id)
            await seed.commit()
            yield _Ctx(
                sessionmaker=factory,
                tenant_id=tenant.id,
                session_id=chat.id,
                message_ids=ids,
            )
    finally:
        db_session._sessionmaker = prev_maker  # noqa: SLF001
        await engine.dispose()


# --- repository -------------------------------------------------------------


async def test_evidence_digest_round_trips_and_clears(ctx: _Ctx) -> None:
    doc, chunk = uuid.uuid4(), uuid.uuid4()
    async with ctx.sessionmaker() as session:
        repo = SessionSummaryRepository(session, ctx.tenant_id)
        assert await repo.get_for_session(ctx.session_id) is None
        row = await repo.upsert_evidence(ctx.session_id, evidence=[(doc, chunk)])
        assert row.evidence == ((doc, chunk),)
        assert row.summary is None and row.version == 0
        # Replaced wholesale; empty clears (a zero-citation answer).
        row = await repo.upsert_evidence(ctx.session_id, evidence=[])
        assert row.evidence == ()
        await session.commit()


async def test_summary_upserts_are_forward_only(ctx: _Ctx) -> None:
    from datetime import datetime

    from sqlalchemy import update

    from app.db import models

    async with ctx.sessionmaker() as session:
        repo = SessionSummaryRepository(session, ctx.tenant_id)
        boundary_new = ctx.message_ids[5]
        boundary_old = ctx.message_ids[2]
        # Make the staleness REAL: SQLite timestamps are second-resolution and
        # the fixture seeds everything in one second, so backdate the old
        # boundary — the guard blocks only STRICTLY older coverage (same-second
        # ties proceed by design; see the repository comment).
        await session.execute(
            update(models.Message)
            .where(models.Message.id == boundary_old)
            .values(created_at=datetime(2000, 1, 1))
        )
        await session.commit()
        rows = await MessageRepository(session, ctx.tenant_id).list_for_session(
            ctx.session_id
        )
        by_id = {m.id: m for m in rows}
        accepted, first = await repo.upsert_summary(
            ctx.session_id,
            summary="v1",
            covers_through_message_id=boundary_new,
            covered_created_at=by_id[boundary_new].created_at,
        )
        assert accepted is True
        assert first is not None and first.version == 1 and first.summary == "v1"
        # A STALE write (strictly older boundary) is a REJECTED no-op — the
        # caller learns it must not audit/bump anything (#446 finding 3).
        stale_accepted, stale = await repo.upsert_summary(
            ctx.session_id,
            summary="stale",
            covers_through_message_id=boundary_old,
            covered_created_at=by_id[boundary_old].created_at,
        )
        assert stale_accepted is False
        assert stale is not None and stale.summary == "v1" and stale.version == 1
        await session.commit()


async def test_malformed_stored_evidence_is_tolerated(ctx: _Ctx) -> None:
    from sqlalchemy import update

    from app.db import models

    async with ctx.sessionmaker() as session:
        repo = SessionSummaryRepository(session, ctx.tenant_id)
        await repo.upsert_evidence(ctx.session_id, evidence=[(uuid.uuid4(), uuid.uuid4())])
        await session.execute(
            update(models.SessionSummary)
            .where(models.SessionSummary.session_id == ctx.session_id)
            .values(evidence=[{"document_id": "not-a-uuid"}, "garbage", 7])
        )
        await session.commit()
        row = await repo.get_for_session(ctx.session_id)
        assert row is not None and row.evidence == ()  # bad entries stripped


# --- the summarizer task ----------------------------------------------------


class _FakeGateway:
    """Deterministic summarizer: records the prompt, returns a fixed summary."""

    last_user_block: str | None = None

    def __init__(self, _settings: object) -> None:
        pass

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        _FakeGateway.last_user_block = messages[-1].content
        return Completion(
            content="SUMMARY: goals and answers so far.",
            model=model or "m",
            usage=TokenUsage(prompt_tokens=50, completion_tokens=20, total_tokens=70),
        )


class _BoomGateway(_FakeGateway):
    async def chat(self, *args: object, **kwargs: object) -> Completion:
        raise RuntimeError("summarizer provider down")


def _summary_settings(**overrides: object) -> object:
    from app.core.config import get_settings

    settings = get_settings()
    # Pin the #416 knobs the task reads regardless of the ambient env.
    return settings.model_copy(
        update={
            "chat_summary_keep_messages": overrides.get("keep", 4),
            "chat_summary_min_batch": overrides.get("min_batch", 4),
            "chat_summary_model": "",
        }
    )


async def test_task_skips_below_batch_threshold(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.tasks import summarize as task_module

    monkeypatch.setattr(task_module, "LLMGateway", _FakeGateway)
    monkeypatch.setattr(
        task_module, "get_settings", lambda: _summary_settings(keep=12, min_batch=4)
    )
    # 14 messages, keep 12 → only 2 beyond the tail < min_batch 4 → skip.
    outcome = await task_module._summarize(ctx.tenant_id, ctx.session_id)  # noqa: SLF001
    assert outcome == "skipped_below_batch"
    async with ctx.sessionmaker() as session:
        assert (
            await SessionSummaryRepository(session, ctx.tenant_id).get_for_session(
                ctx.session_id
            )
            is None
        )


async def test_task_summarizes_preserving_the_verbatim_tail(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.tasks import summarize as task_module

    monkeypatch.setattr(task_module, "LLMGateway", _FakeGateway)
    monkeypatch.setattr(
        task_module, "get_settings", lambda: _summary_settings(keep=4, min_batch=4)
    )
    outcome = await task_module._summarize(ctx.tenant_id, ctx.session_id)  # noqa: SLF001
    assert outcome == "summarized"
    # Covered = the 10 oldest (14 - keep 4); the boundary is message index 9.
    async with ctx.sessionmaker() as session:
        row = await SessionSummaryRepository(session, ctx.tenant_id).get_for_session(
            ctx.session_id
        )
        assert row is not None
        assert row.summary == "SUMMARY: goals and answers so far."
        assert row.covers_through_message_id == ctx.message_ids[9]
        assert row.version == 1
        # The verbatim tail's content never reached the summarizer prompt…
        assert _FakeGateway.last_user_block is not None
        assert "turn 9" in _FakeGateway.last_user_block
        assert "turn 10" not in _FakeGateway.last_user_block
        # …and the write was audited + its spend recorded (INV-6 / §2.6).
        recent = await AuditEventRepository(session, ctx.tenant_id).list_recent(limit=20)
        summarized = [e for e in recent if e.action == "session.summarized"]
        assert summarized and summarized[0].metadata["covered_messages"] == 10
        usage_rows = await LlmUsageRepository(session, ctx.tenant_id).list_for_session(
            ctx.session_id
        )
        assert usage_rows and usage_rows[-1].total_tokens == 70


async def test_task_second_pass_rolls_forward(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.tasks import summarize as task_module

    monkeypatch.setattr(task_module, "LLMGateway", _FakeGateway)
    monkeypatch.setattr(
        task_module, "get_settings", lambda: _summary_settings(keep=4, min_batch=2)
    )
    assert await task_module._summarize(ctx.tenant_id, ctx.session_id) == "summarized"  # noqa: SLF001
    # Four MORE turns arrive; the next pass folds the previously-kept tail.
    async with ctx.sessionmaker() as session:
        messages = MessageRepository(session, ctx.tenant_id)
        for i in range(14, 18):
            await messages.add(
                session_id=ctx.session_id,
                role=MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT,
                content=f"turn {i}",
            )
        await session.commit()
    assert await task_module._summarize(ctx.tenant_id, ctx.session_id) == "summarized"  # noqa: SLF001
    async with ctx.sessionmaker() as session:
        row = await SessionSummaryRepository(session, ctx.tenant_id).get_for_session(
            ctx.session_id
        )
        assert row is not None and row.version == 2
        # The PREVIOUS summary was fed forward into the second pass's prompt.
        assert _FakeGateway.last_user_block is not None
        assert "Previous summary:" in _FakeGateway.last_user_block


async def test_dead_gateway_leaves_summary_state_untouched(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4 (async half): a dead summarizer provider raises out of the body —
    and nothing about the session's summary state changed."""
    from app.tasks import summarize as task_module

    monkeypatch.setattr(task_module, "LLMGateway", _BoomGateway)
    monkeypatch.setattr(
        task_module, "get_settings", lambda: _summary_settings(keep=4, min_batch=4)
    )
    with pytest.raises(RuntimeError):
        await task_module._summarize(ctx.tenant_id, ctx.session_id)  # noqa: SLF001
    async with ctx.sessionmaker() as session:
        assert (
            await SessionSummaryRepository(session, ctx.tenant_id).get_for_session(
                ctx.session_id
            )
            is None
        )


def test_wrapper_contains_every_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-4 (sync half): the Celery entrypoint CONTAINS any body failure — a
    logged outcome dict, never a raise (the answer already streamed)."""
    from app.tasks import summarize as task_module

    def boom_run_task(coro: object) -> object:
        coro.close()  # type: ignore[attr-defined]
        raise RuntimeError("provider down")

    monkeypatch.setattr(task_module, "run_task", boom_run_task)
    result = task_module.summarize_session(str(uuid.uuid4()), str(uuid.uuid4()))
    assert result == {"outcome": "failed", "error_type": "RuntimeError"}


# --- #446 round-2: concurrency, tenancy, shed, redaction ---------------------


async def test_concurrent_upserts_converge_without_errors(ctx: _Ctx) -> None:
    """#446 finding 3: an answer's evidence write racing a summarizer's claim
    on a fresh row must never surface a unique-key error — the atomic upserts
    converge on one row carrying both columns."""
    import asyncio as _asyncio

    doc, chunk = uuid.uuid4(), uuid.uuid4()

    async def write_evidence() -> None:
        async with ctx.sessionmaker() as session:
            await SessionSummaryRepository(session, ctx.tenant_id).upsert_evidence(
                ctx.session_id, evidence=[(doc, chunk)]
            )
            await session.commit()

    async def write_summary() -> None:
        async with ctx.sessionmaker() as session:
            rows = await MessageRepository(session, ctx.tenant_id).list_for_session(
                ctx.session_id
            )
            await SessionSummaryRepository(session, ctx.tenant_id).upsert_summary(
                ctx.session_id,
                summary="racing summary",
                covers_through_message_id=rows[0].id,
                covered_created_at=rows[0].created_at,
            )
            await session.commit()

    await _asyncio.gather(write_evidence(), write_summary())
    async with ctx.sessionmaker() as session:
        row = await SessionSummaryRepository(session, ctx.tenant_id).get_for_session(
            ctx.session_id
        )
    assert row is not None
    assert row.evidence == ((doc, chunk),)
    assert row.summary == "racing summary"


async def test_same_second_advance_to_new_boundary_is_accepted(ctx: _Ctx) -> None:
    """The tie policy, pinned: a same-timestamp advance to a DIFFERENT boundary
    id is accepted (second-resolution stamps must not wedge progress); an
    IDENTICAL rewrite is a rejected no-op (idempotent)."""
    async with ctx.sessionmaker() as session:
        repo = SessionSummaryRepository(session, ctx.tenant_id)
        rows = await MessageRepository(session, ctx.tenant_id).list_for_session(
            ctx.session_id
        )
        same_second = [m for m in rows if m.created_at == rows[0].created_at]
        assert len(same_second) >= 2  # the fixture seeds within one second
        a, b = same_second[0], same_second[1]
        accepted, _ = await repo.upsert_summary(
            ctx.session_id,
            summary="v1",
            covers_through_message_id=a.id,
            covered_created_at=a.created_at,
        )
        assert accepted is True
        accepted, _ = await repo.upsert_summary(
            ctx.session_id,
            summary="v2",
            covers_through_message_id=b.id,
            covered_created_at=b.created_at,
        )
        assert accepted is True  # same second, different boundary — progress
        accepted, row = await repo.upsert_summary(
            ctx.session_id,
            summary="v2-rewrite",
            covers_through_message_id=b.id,
            covered_created_at=b.created_at,
        )
        assert accepted is False  # identical boundary — idempotent no-op
        assert row is not None and row.summary == "v2"
        await session.commit()


async def test_cross_tenant_repository_sees_nothing(ctx: _Ctx) -> None:
    """INV-1: a foreign-tenant repository neither reads nor writes this
    session's memory row."""
    async with ctx.sessionmaker() as session:
        await SessionSummaryRepository(session, ctx.tenant_id).upsert_evidence(
            ctx.session_id, evidence=[(uuid.uuid4(), uuid.uuid4())]
        )
        await session.commit()
        foreign = SessionSummaryRepository(session, uuid.uuid4())
        assert await foreign.get_for_session(ctx.session_id) is None


async def test_evidence_and_summary_caps_hold_at_write(ctx: _Ctx) -> None:
    """#446 finding 4: the digest caps at 20 pairs and the summary at 6000
    chars — at the WRITE chokepoint, so no stored row can fan out unbounded."""
    async with ctx.sessionmaker() as session:
        repo = SessionSummaryRepository(session, ctx.tenant_id)
        many = [(uuid.uuid4(), uuid.uuid4()) for _ in range(50)]
        row = await repo.upsert_evidence(ctx.session_id, evidence=many)
        assert row is not None and len(row.evidence) == 20
        rows = await MessageRepository(session, ctx.tenant_id).list_for_session(
            ctx.session_id
        )
        _, srow = await repo.upsert_summary(
            ctx.session_id,
            summary="x" * 20_000,
            covers_through_message_id=rows[0].id,
            covered_created_at=rows[0].created_at,
        )
        assert srow is not None and srow.summary is not None
        assert len(srow.summary) == 6_000
        await session.commit()


def test_redaction_removes_cited_snippets() -> None:
    """#446 finding 1: the deterministic write-side backstop — verbatim cited
    spans are cut from the summarizer's output; paraphrase survives."""
    from app.tasks.summarize import _redact_cited_snippets

    snippet = "The 2024 standard deduction for single filers is $14,600."
    out = _redact_cited_snippets(
        f"They discussed taxes. The doc said: {snippet} Then they moved on.",
        [snippet],
    )
    assert snippet not in out
    assert "[cited source text removed]" in out
    assert "They discussed taxes." in out


async def test_summary_route_resolves_provider_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#446 finding 6: a provider-only session summarizes through the SAME
    provider-route seam as chat; an unresolved provider degrades to the config
    default instead of failing the task."""
    from app.services.provider_models import ModelRoute
    from app.tasks import summarize as task_module

    settings = _summary_settings()

    def fake_builder(**_kwargs: object) -> object:
        async def resolver(_session: object, model_id: str) -> ModelRoute:
            if model_id == "provider:abc:their/model":
                return ModelRoute(
                    model="their/model", api_key="tenant-key", api_base="http://p"
                )
            return ModelRoute(model=model_id)  # unresolved passthrough

        return resolver

    monkeypatch.setattr(task_module, "build_model_route_resolver", fake_builder)
    route = await task_module._resolve_summary_route(  # noqa: SLF001
        object(),
        settings,  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        session_model="provider:abc:their/model",
    )
    assert route.model == "their/model"
    assert route.api_key == "tenant-key"
    ghost = await task_module._resolve_summary_route(  # noqa: SLF001
        object(),
        settings,  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        session_model="provider:ghost:gone/model",
    )
    assert not ghost.model.startswith("provider:")
    assert ghost.api_key is None
