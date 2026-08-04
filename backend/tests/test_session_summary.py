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
            # Distinct ascending stamps (a real session spans time; SQLite's
            # server default would land everything in one second and make
            # index order diverge from the (created_at, id) total order).
            from datetime import datetime as _dt

            from sqlalchemy import update as _upd

            from app.db import models as _models

            for i, mid in enumerate(ids):
                await seed.execute(
                    _upd(_models.Message)
                    .where(_models.Message.id == mid)
                    .values(created_at=_dt(2021, 1, 1, 0, i))
                )
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
        rows = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
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


async def test_task_skips_below_batch_threshold(ctx: _Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
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
            await SessionSummaryRepository(session, ctx.tenant_id).get_for_session(ctx.session_id)
            is None
        )


async def test_task_summarizes_preserving_the_verbatim_tail(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.tasks import summarize as task_module

    monkeypatch.setattr(task_module, "LLMGateway", _FakeGateway)
    monkeypatch.setattr(task_module, "get_settings", lambda: _summary_settings(keep=4, min_batch=4))
    outcome = await task_module._summarize(ctx.tenant_id, ctx.session_id)  # noqa: SLF001
    assert outcome == "summarized"
    # Covered = the 10 oldest (14 - keep 4); the boundary is message index 9.
    async with ctx.sessionmaker() as session:
        row = await SessionSummaryRepository(session, ctx.tenant_id).get_for_session(ctx.session_id)
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


async def test_task_second_pass_rolls_forward(ctx: _Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime as _dt

    from sqlalchemy import update as _upd

    from app.db import models as _models
    from app.tasks import summarize as task_module

    monkeypatch.setattr(task_module, "LLMGateway", _FakeGateway)
    monkeypatch.setattr(task_module, "get_settings", lambda: _summary_settings(keep=4, min_batch=2))
    # Deterministic eras: the seeded 14 turns land minutes in the past so the
    # second pass's boundary is STRICTLY newer (the same-second tie-break is a
    # stable-but-arbitrary id order — correct for racing tasks, coin-floppy
    # for a test that seeds everything within one wall-clock second).
    async with ctx.sessionmaker() as session:
        for i, mid in enumerate(ctx.message_ids):
            await session.execute(
                _upd(_models.Message)
                .where(_models.Message.id == mid)
                .values(created_at=_dt(2020, 1, 1, 0, i))
            )
        await session.commit()
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
        row = await SessionSummaryRepository(session, ctx.tenant_id).get_for_session(ctx.session_id)
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
    monkeypatch.setattr(task_module, "get_settings", lambda: _summary_settings(keep=4, min_batch=4))
    with pytest.raises(RuntimeError):
        await task_module._summarize(ctx.tenant_id, ctx.session_id)  # noqa: SLF001
    async with ctx.sessionmaker() as session:
        assert (
            await SessionSummaryRepository(session, ctx.tenant_id).get_for_session(ctx.session_id)
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
            rows = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
            await SessionSummaryRepository(session, ctx.tenant_id).upsert_summary(
                ctx.session_id,
                summary="racing summary",
                covers_through_message_id=rows[0].id,
                covered_created_at=rows[0].created_at,
            )
            await session.commit()

    await _asyncio.gather(write_evidence(), write_summary())
    async with ctx.sessionmaker() as session:
        row = await SessionSummaryRepository(session, ctx.tenant_id).get_for_session(ctx.session_id)
    assert row is not None
    assert row.evidence == ((doc, chunk),)
    assert row.summary == "racing summary"


async def test_same_second_ties_advance_only_in_id_order(ctx: _Ctx) -> None:
    """The ORDERED tie policy (#446 r2 blocker 3), pinned: within one second,
    coverage advances only toward the LARGER boundary id — so the A→B→A
    sequence can never re-accept A; an identical rewrite is a rejected no-op;
    and the smaller-id direction is rejected outright (it converges on the
    next strictly-newer-timestamp pass)."""
    async with ctx.sessionmaker() as session:
        repo = SessionSummaryRepository(session, ctx.tenant_id)
        from datetime import datetime as _dt

        from sqlalchemy import update as _upd

        from app.db import models as _models

        messages_repo = MessageRepository(session, ctx.tenant_id)
        pair = []
        for i in range(2):
            pair.append(
                await messages_repo.add(
                    session_id=ctx.session_id,
                    role=MessageRole.USER,
                    content=f"tie {i}",
                )
            )
        stamp = _dt(2022, 6, 1, 12, 0, 0)
        for m in pair:
            await session.execute(
                _upd(_models.Message).where(_models.Message.id == m.id).values(created_at=stamp)
            )
        await session.commit()
        rows = await messages_repo.list_for_session(ctx.session_id)
        same_second = [m for m in rows if m.created_at == stamp]
        assert len(same_second) == 2
        # Sorted by STORAGE order (undashed hex ≡ sqlite CHAR(32); pg uuid).
        small, large = sorted(same_second, key=lambda m: m.id.hex)
        accepted, _ = await repo.upsert_summary(
            ctx.session_id,
            summary="A",
            covers_through_message_id=small.id,
            covered_created_at=small.created_at,
        )
        assert accepted is True
        accepted, _ = await repo.upsert_summary(
            ctx.session_id,
            summary="B",
            covers_through_message_id=large.id,
            covered_created_at=large.created_at,
        )
        assert accepted is True  # tie, larger id — forward
        # The round-2 probe: a STALE same-second write back to A must be a
        # rejected no-op — never re-accepted, never audited.
        accepted, row = await repo.upsert_summary(
            ctx.session_id,
            summary="A-stale",
            covers_through_message_id=small.id,
            covered_created_at=small.created_at,
        )
        assert accepted is False
        assert row is not None and row.summary == "B"
        # Identical rewrite: rejected no-op (idempotent).
        accepted, row = await repo.upsert_summary(
            ctx.session_id,
            summary="B-rewrite",
            covers_through_message_id=large.id,
            covered_created_at=large.created_at,
        )
        assert accepted is False
        assert row is not None and row.summary == "B"
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
        rows = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
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
                return ModelRoute(model="their/model", api_key="tenant-key", api_base="http://p")
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


async def test_summary_route_uses_fast_default_not_the_frontier_session_model() -> None:
    """#490 AC-2: an unpinned summarizer runs on the DEDICATED FAST default, not
    the session's answer route. A background compaction task must never inherit a
    frontier model just because the chat session is pinned to one — that is the
    exact leak #490 closes for config (non-``provider:``) sessions."""
    from app.tasks import summarize as task_module

    settings = _summary_settings()  # chat_summary_model="" (unpinned)
    fast_default = next(m.id for m in settings.chat_model_registry if m.is_default)
    # A frontier config session model — the summarizer must NOT follow it.
    route = await task_module._resolve_summary_route(  # noqa: SLF001
        object(),
        settings,  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        session_model="openrouter/anthropic/claude-opus-4.8",
    )
    assert route.model == fast_default
    assert route.api_key is None


async def test_summary_route_honors_a_pinned_dedicated_model() -> None:
    """#490 AC-2: a config-pinned ``CHAT_SUMMARY_MODEL`` is the requested id,
    distinct from both the session's answer route and the registry default."""
    from app.core.config import get_settings
    from app.tasks import summarize as task_module

    pinned = "openrouter/google/gemini-3.5-flash"
    settings = get_settings().model_copy(
        update={
            "chat_summary_keep_messages": 4,
            "chat_summary_min_batch": 4,
            "chat_summary_model": pinned,
        }
    )
    route = await task_module._resolve_summary_route(  # noqa: SLF001
        object(),
        settings,  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        session_model="openrouter/anthropic/claude-opus-4.8",
    )
    assert route.model == pinned


async def test_mention_map_holds_forty_entries(ctx: _Ctx) -> None:
    """#446 round-3 blocker-1 gate: the mention map stores and reads back up to
    40 entries — a 25-name summary keeps EVERY name redactable (the >20-name
    revocation gap is closed at both write and defensive read)."""
    async with ctx.sessionmaker() as session:
        repo = SessionSummaryRepository(session, ctx.tenant_id)
        rows = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        mentioned = {uuid.uuid4(): f"doc-{i}.pdf" for i in range(25)}
        accepted, srow = await repo.upsert_summary(
            ctx.session_id,
            summary="mentions many documents",
            covers_through_message_id=rows[0].id,
            covered_created_at=rows[0].created_at,
            mentioned_documents=mentioned,
        )
        await session.commit()
        assert accepted is True
        assert srow is not None
        assert len(srow.mentioned_documents) == 25
        stored_names = {name for _id, name in srow.mentioned_documents}
        assert "doc-24.pdf" in stored_names  # entry 21+ survives


async def test_same_second_backlog_converges_across_passes(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#446 round-3 major-5 gate (the 100-same-second starvation probe): the
    batch selection and the CAS advance in the SAME (created_at, id) order,
    so repeated passes fold the whole backlog — versions strictly increase,
    no pass is skipped as stale, and coverage completes."""
    from app.tasks import summarize as task_module

    monkeypatch.setattr(task_module, "LLMGateway", _FakeGateway)
    monkeypatch.setattr(task_module, "get_settings", lambda: _summary_settings(keep=4, min_batch=2))
    # 100 messages in ONE second (the fixture's 14 + 86 more).
    async with ctx.sessionmaker() as session:
        messages = MessageRepository(session, ctx.tenant_id)
        for i in range(14, 100):
            await messages.add(
                session_id=ctx.session_id,
                role=MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT,
                content=f"turn {i}",
            )
        await session.commit()

    outcomes = []
    versions = []
    for _ in range(6):
        outcomes.append(await task_module._summarize(ctx.tenant_id, ctx.session_id))  # noqa: SLF001
        async with ctx.sessionmaker() as session:
            row = await SessionSummaryRepository(session, ctx.tenant_id).get_for_session(
                ctx.session_id
            )
        versions.append(row.version if row is not None else 0)

    # The 96 coverable messages (100 - keep 4) fold in 40+40+16 = three passes;
    # later passes skip BELOW BATCH (nothing left), never as stale.
    assert outcomes[:3] == ["summarized", "summarized", "summarized"]
    assert "skipped_stale_coverage" not in outcomes
    assert versions[:3] == [1, 2, 3]
    assert versions[-1] == 3


# --- #491 AC-2: the retuned defaults reach first compaction earlier ----------


def test_default_summary_thresholds_reach_first_compaction_before_turn_seven() -> None:
    """AC-2: the retuned defaults make the FIRST summarisation fire earlier.

    The first summarise fires once ``keep + min_batch`` messages have
    accumulated. The old 8 + 4 = 12 needed ~6 turns (a 2-msg/turn session) —
    "roughly turn 7" in #491. The retuned 4 + 2 = 6 fires at ~turn 3, well
    before turn 7. Pinned on the FIELD defaults so ambient env cannot mask a
    regression of the code default.
    """
    from app.core.config import Settings

    keep = Settings.model_fields["chat_summary_keep_messages"].default
    min_batch = Settings.model_fields["chat_summary_min_batch"].default
    assert keep == 4
    assert min_batch == 2
    first_compaction_messages = keep + min_batch
    assert first_compaction_messages == 6  # ~turn 3, a 2-msg/turn session
    assert first_compaction_messages < 12  # strictly earlier than the old ~turn 7


@pytest_asyncio.fixture
async def six_message_ctx() -> AsyncIterator[_Ctx]:
    """A 3-turn (6-message) session — the smallest the retuned defaults summarise."""
    from datetime import datetime as _dt

    from sqlalchemy import update as _upd

    from app.db import models as _models

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    prev_maker = db_session._sessionmaker  # noqa: SLF001
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        db_session._sessionmaker = factory  # noqa: SLF001
        async with factory() as seed:
            tenant = await TenantRepository(seed).create(name="Acme")
            user = await UserRepository(seed, tenant.id).create(
                email="bob@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            chat = await ChatSessionRepository(seed, tenant.id).create(
                owner_id=user.id, model="m", title="t"
            )
            messages = MessageRepository(seed, tenant.id)
            ids: list[uuid.UUID] = []
            for i in range(6):
                role = MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT
                m = await messages.add(
                    session_id=chat.id, role=role, content=f"turn {i}: the sky is {i}"
                )
                ids.append(m.id)
            for i, mid in enumerate(ids):
                await seed.execute(
                    _upd(_models.Message)
                    .where(_models.Message.id == mid)
                    .values(created_at=_dt(2021, 1, 1, 0, i))
                )
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


async def test_six_message_session_summarises_under_retuned_defaults(
    six_message_ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2 behavioural: a 3-turn session summarises under the retuned defaults
    (keep=4, min_batch=2) but would have SKIPPED under the old (keep=8, min_batch
    =4) — the same 6 messages did not reach the old ~turn-7 threshold."""
    from app.tasks import summarize as task_module

    monkeypatch.setattr(task_module, "LLMGateway", _FakeGateway)

    # Old thresholds on the SAME 6-message session: below batch → no summary.
    monkeypatch.setattr(task_module, "get_settings", lambda: _summary_settings(keep=8, min_batch=4))
    old_outcome = await task_module._summarize(  # noqa: SLF001
        six_message_ctx.tenant_id, six_message_ctx.session_id
    )
    assert old_outcome == "skipped_below_batch"

    # Retuned defaults: 6 - keep 4 = 2 uncovered >= min_batch 2 → summarises.
    monkeypatch.setattr(task_module, "get_settings", lambda: _summary_settings(keep=4, min_batch=2))
    new_outcome = await task_module._summarize(  # noqa: SLF001
        six_message_ctx.tenant_id, six_message_ctx.session_id
    )
    assert new_outcome == "summarized"
    async with six_message_ctx.sessionmaker() as session:
        row = await SessionSummaryRepository(session, six_message_ctx.tenant_id).get_for_session(
            six_message_ctx.session_id
        )
        assert row is not None and row.version == 1
        # keep=4 preserves the last 2 turns (messages 2-5) verbatim; turns 0-1
        # (messages 0-1) roll into the summary.
        assert row.covers_through_message_id == six_message_ctx.message_ids[1]


# --- #541: a garbage completion must not advance the cursor -------------------


class _GarbageGateway(_FakeGateway):
    """A route that leaks chat-template scaffolding instead of writing a summary.

    Not hypothetical: `openrouter/openai/gpt-oss-20b:free` returned exactly this for a
    seven-turn batch on the live stack, and it was persisted as the session summary.
    """

    payload = "<|start|>spire"

    async def chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        await super().chat(messages, **kwargs)
        return Completion(
            content=self.payload,
            model="fake",
            usage=TokenUsage(prompt_tokens=50, completion_tokens=20, total_tokens=70),
        )


async def test_a_control_token_completion_is_refused_and_the_cursor_stays_put(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing is strictly better than accepting: the turns stay verbatim.

    Accepting is NOT a no-op — `upsert_summary` advances `covers_through_message_id`,
    so the covered turns stop riding verbatim and are represented only by what was
    stored. A 14-character protocol artifact standing in for ten turns is silent,
    irreversible context loss; leaving them verbatim merely costs tokens until the
    next attempt succeeds.
    """
    from app.tasks import summarize as task_module

    monkeypatch.setattr(task_module, "LLMGateway", _GarbageGateway)
    monkeypatch.setattr(task_module, "get_settings", lambda: _summary_settings(keep=4, min_batch=4))

    outcome = await task_module._summarize(ctx.tenant_id, ctx.session_id)  # noqa: SLF001

    assert outcome == "skipped_implausible_summary"
    async with ctx.sessionmaker() as session:
        row = await SessionSummaryRepository(session, ctx.tenant_id).get_for_session(ctx.session_id)
        # No row at all, or one that never advanced — either way the turns are intact.
        assert row is None or row.covers_through_message_id is None
        # A refusal IS audited — but as an error carrying the reason, never as a
        # successful summarisation (that distinction is the point: the operator can
        # find a session burning tokens without producing a usable summary).
        recent = await AuditEventRepository(session, ctx.tenant_id).list_recent(limit=20)
        summarized = [e for e in recent if e.action == "session.summarized"]
        assert summarized, "the refusal left no audit trail at all"
        assert all(e.outcome == "error" for e in summarized)
        assert all(e.metadata.get("refused") for e in summarized)


async def test_a_completion_under_the_absolute_floor_is_refused(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No control token — just below the absolute character floor."""
    from app.tasks import summarize as task_module

    class _Tiny(_GarbageGateway):
        payload = "ok"

    monkeypatch.setattr(task_module, "LLMGateway", _Tiny)
    monkeypatch.setattr(task_module, "get_settings", lambda: _summary_settings(keep=4, min_batch=4))

    outcome = await task_module._summarize(ctx.tenant_id, ctx.session_id)  # noqa: SLF001
    assert outcome == "skipped_implausible_summary"


async def test_a_completion_over_the_floor_but_tiny_for_many_turns_is_refused(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MULTI-TURN branch, which review found had no test reaching it at all.

    Both other refusal tests trip earlier rules (control token, absolute floor), so
    this branch was live code with no coverage. 20 chars clears the 16-char floor but
    cannot represent ten turns.
    """
    from app.tasks import summarize as task_module

    class _Terse(_GarbageGateway):
        payload = "Talked about stuff"  # 18 chars: over the 16 floor, under the 32 multi-turn bar

    monkeypatch.setattr(task_module, "LLMGateway", _Terse)
    monkeypatch.setattr(task_module, "get_settings", lambda: _summary_settings(keep=4, min_batch=4))

    outcome = await task_module._summarize(ctx.tenant_id, ctx.session_id)  # noqa: SLF001
    assert outcome == "skipped_implausible_summary"


async def test_the_bar_never_exceeds_what_the_model_is_allowed_to_write(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compliant summary must pass no matter how LONG the covered turns were.

    An earlier cut scaled the minimum against the covered TEXT (>=1% of input). At the
    40-message batch cap with this deployment's p95 message length that demanded ~1,662
    chars, while the prompt asks for "under 300 words" and `max_tokens=600` caps output
    around 2,400 — so a compliant summary was refused, and past ~240k chars of input no
    completion could pass at all, leaving the session permanently unsummarisable. The
    bar is now fixed per turn-count, so enormous turns cannot move it.
    """
    from sqlalchemy import update as _upd

    from app.db import models as _models
    from app.tasks import summarize as task_module

    # Make the covered turns enormous — the exact condition that broke the old rule.
    async with ctx.sessionmaker() as session:
        await session.execute(
            _upd(_models.Message)
            .where(_models.Message.session_id == ctx.session_id)
            .values(content="x" * 6000)
        )
        await session.commit()

    class _Compliant(_GarbageGateway):
        # ~200 chars: far under "300 words", comfortably a real summary.
        payload = (
            "The user asked about regional revenue and the assistant walked through "
            "the northern and southern figures, flagged one unresolved attribution "
            "question, and agreed to follow up next week."
        )

    monkeypatch.setattr(task_module, "LLMGateway", _Compliant)
    monkeypatch.setattr(task_module, "get_settings", lambda: _summary_settings(keep=4, min_batch=4))

    outcome = await task_module._summarize(ctx.tenant_id, ctx.session_id)  # noqa: SLF001
    assert outcome == "summarized", "a compliant summary was refused for long input"


async def test_a_refused_summary_still_records_its_spend_and_audits(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal is not free, and it repeats — so it must be visible.

    The completion was paid for, and because the cursor does not advance the same batch
    is re-sent on every subsequent answer. Without a usage row and an audit event, a
    tenant on a junk route pays a full summariser prompt per answer forever with
    nothing to find it by (ADR-0016 §2.6: no invisible spend; spec 0004 §2.4 puts ops
    logging outside the product audit trail).
    """
    from app.tasks import summarize as task_module

    monkeypatch.setattr(task_module, "LLMGateway", _GarbageGateway)
    monkeypatch.setattr(task_module, "get_settings", lambda: _summary_settings(keep=4, min_batch=4))

    outcome = await task_module._summarize(ctx.tenant_id, ctx.session_id)  # noqa: SLF001
    assert outcome == "skipped_implausible_summary"

    async with ctx.sessionmaker() as session:
        usage = await LlmUsageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assert usage and usage[-1].total_tokens == 70, "refused spend went unrecorded"
        recent = await AuditEventRepository(session, ctx.tenant_id).list_recent(limit=20)
        refusals = [
            e for e in recent if e.action == "session.summarized" and e.metadata.get("refused")
        ]
        assert refusals, "the refusal was invisible in the audit trail"
        assert refusals[0].outcome == "error"


async def test_a_genuine_summary_is_still_accepted(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the guard must not make the feature unusable.

    Without this, "reject everything" would pass the two tests above.
    """
    from app.tasks import summarize as task_module

    class _Real(_GarbageGateway):
        payload = (
            "The user asked about Q3 revenue across regions. The assistant reported a "
            "12% rise in the northern region driven by two renewals, flat performance "
            "in the south, and an unresolved question about west-region attribution."
        )

    monkeypatch.setattr(task_module, "LLMGateway", _Real)
    monkeypatch.setattr(task_module, "get_settings", lambda: _summary_settings(keep=4, min_batch=4))

    outcome = await task_module._summarize(ctx.tenant_id, ctx.session_id)  # noqa: SLF001
    assert outcome == "summarized"
    async with ctx.sessionmaker() as session:
        row = await SessionSummaryRepository(session, ctx.tenant_id).get_for_session(ctx.session_id)
        assert row is not None and row.covers_through_message_id == ctx.message_ids[9]


class _EmptyGateway(_GarbageGateway):
    """A route that returns nothing at all — a truncated stream, a filtered response."""

    payload = "   "


async def test_an_empty_completion_is_as_expensive_as_a_refusal(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit beside the quality gate had the same hole, and round 2 found it.

    `_record_summary_spend` was wired to the implausible-summary branch alone. An empty
    completion returned bare one line earlier: the prompt tokens were still paid, the
    cursor still did not advance, so the same batch was re-sent on every answer — the
    identical invisible-spend-plus-forever-retry the refusal path exists to close, left
    open on the adjacent branch. A fix that covers one exit and not its neighbour is not
    a fix; it just moves where the money disappears.
    """
    from app.tasks import summarize as task_module

    monkeypatch.setattr(task_module, "LLMGateway", _EmptyGateway)
    monkeypatch.setattr(task_module, "get_settings", lambda: _summary_settings(keep=4, min_batch=4))

    outcome = await task_module._summarize(ctx.tenant_id, ctx.session_id)  # noqa: SLF001
    assert outcome == "skipped_empty_summary"

    async with ctx.sessionmaker() as session:
        usage = await LlmUsageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assert usage and usage[-1].total_tokens == 70, "an empty completion was billed to nobody"
        recent = await AuditEventRepository(session, ctx.tenant_id).list_recent(limit=20)
        refusals = [
            e for e in recent if e.action == "session.summarized" and e.metadata.get("refused")
        ]
        assert refusals, "the empty completion was invisible in the audit trail"
        assert refusals[0].outcome == "error"
        assert "empty" in refusals[0].metadata["refused"]
        # And the turns it failed to summarise are still riding verbatim.
        summary = await SessionSummaryRepository(session, ctx.tenant_id).get_for_session(
            ctx.session_id
        )
        assert summary is None or summary.covers_through_message_id is None
