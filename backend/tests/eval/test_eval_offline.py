"""Offline eval run — the golden set scored deterministically in CI (#29).

Drives the **whole** golden Q/A-with-source set through the real chat runtime
with a faked grounded gateway and the deterministic permission-filtered
retrieval, then asserts the aggregate metrics clear the (perfect) offline
thresholds. This is the CI-exercised proof that the harness works and that the
loop, on a known corpus, is grounded + correctly cited (ADR-0003 §9).

It also asserts the two quality negatives end-to-end (not just at the metric
level): the honest-refusal question yields a zero-citation refusal, and a
deliberately ungrounded answer is caught by the aggregate gate.

No key, no Postgres, no Redis — in-memory SQLite + in-memory backplane.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.db.base import Base
from app.db.repositories import (
    ChatSessionRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import Role
from app.domain.llm import StreamEvent
from tests.eval.golden import GOLDEN_DOCUMENTS, GOLDEN_QUESTIONS, GoldenQuestion
from tests.eval.harness import answer_question, grounded_gateway, run_eval, seed_corpus
from tests.eval.scoring import Thresholds, aggregate, assert_meets, score_item
from tests.eval.support import DeterministicEmbedder, OfflineRetrieval

import app.db.models  # noqa: F401  isort: skip

_MODEL = "anthropic/claude-opus-4.8"
_REFUSAL = "I couldn't find anything in your sources that answers that."


class _EvalCtx:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        retrieval_session: AsyncSession,
        principal: Principal,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.retrieval_session = retrieval_session
        self.principal = principal


@pytest_asyncio.fixture
async def eval_ctx() -> AsyncIterator[_EvalCtx]:
    """A seeded in-memory DB: one tenant + user owning the whole golden corpus.

    Also yields a single managed read session for the offline retrieval (closed in
    teardown), so no session is leaked and the ``filterwarnings = error`` gate
    stays clean.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        embedder = DeterministicEmbedder()
        async with factory() as seed:
            tenant = await TenantRepository(seed).create(name="Acme")
            user = await UserRepository(seed, tenant.id).create(
                email="kw@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            await seed_corpus(
                seed,
                tenant_id=tenant.id,
                owner_id=user.id,
                documents=GOLDEN_DOCUMENTS,
                embed=embedder.embed,
            )
            await seed.commit()
            principal = Principal(user_id=user.id, tenant_id=tenant.id, roles=(Role.MEMBER,))
        async with factory() as retrieval_session:
            yield _EvalCtx(
                sessionmaker=factory,
                retrieval_session=retrieval_session,
                principal=principal,
            )
    finally:
        await engine.dispose()


def _session_factory(ctx: _EvalCtx) -> Callable[[], Awaitable[uuid.UUID]]:
    async def _new_session_id() -> uuid.UUID:
        async with ctx.sessionmaker() as session:
            row = await ChatSessionRepository(session, ctx.principal.tenant_id).create(
                owner_id=ctx.principal.user_id, model=_MODEL, title="eval"
            )
            await session.commit()
            return row.id

    return _new_session_id


def _retrieval(ctx: _EvalCtx) -> OfflineRetrieval:
    return OfflineRetrieval(ctx.retrieval_session, embedder=DeterministicEmbedder())


async def test_offline_eval_meets_thresholds(eval_ctx: _EvalCtx) -> None:
    """The full golden set scores at the offline (perfect) bar — the harness proof."""
    scores = await run_eval(
        sessionmaker=eval_ctx.sessionmaker,
        retrieval=_retrieval(eval_ctx),
        gateway=grounded_gateway(refusal_text=_REFUSAL),
        principal=eval_ctx.principal,
        new_session_id=_session_factory(eval_ctx),
        model=_MODEL,
        refusal_text=_REFUSAL,
    )
    score = aggregate(scores)
    # Every metric must be perfect on the deterministic corpus.
    assert_meets(score, Thresholds())
    assert score.groundedness == 1.0, score.summary()
    assert score.citation_correctness == 1.0, score.summary()
    assert score.retrieval_recall == 1.0, score.summary()


async def test_offline_eval_refusal_item_has_zero_citations(eval_ctx: _EvalCtx) -> None:
    """The unanswerable question yields a grounded, zero-citation refusal (AC-3)."""
    unanswerable: GoldenQuestion = next(q for q in GOLDEN_QUESTIONS if not q.is_answerable)
    new_session_id = _session_factory(eval_ctx)
    session_id = await new_session_id()
    observed = await answer_question(
        sessionmaker=eval_ctx.sessionmaker,
        retrieval=_retrieval(eval_ctx),
        gateway=grounded_gateway(refusal_text=_REFUSAL),
        principal=eval_ctx.principal,
        session_id=session_id,
        question=unanswerable,
        model=_MODEL,
        refusal_text=_REFUSAL,
    )
    assert observed.citations == ()
    item = score_item(unanswerable, observed)
    assert item.grounded is True
    assert item.citation_correct is True


async def test_offline_eval_catches_an_ungrounded_answer(eval_ctx: _EvalCtx) -> None:
    """A deliberately ungrounded gateway (asserts a fact, never searches) FAILS.

    Proves the gate has teeth: a model that answers from "prior knowledge"
    without retrieving — so the runtime attaches no citation — drops groundedness
    below the threshold and ``assert_meets`` raises.
    """

    class _UngroundedGateway:
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
            # Never calls a tool; just asserts a fact → zero citations → ungrounded.
            yield StreamEvent(text="The standard deduction is $14,600.")
            yield StreamEvent(finish_reason="stop")

    scores = await run_eval(
        sessionmaker=eval_ctx.sessionmaker,
        retrieval=_retrieval(eval_ctx),
        gateway=_UngroundedGateway(),
        principal=eval_ctx.principal,
        new_session_id=_session_factory(eval_ctx),
        model=_MODEL,
        questions=[q for q in GOLDEN_QUESTIONS if q.is_answerable],
    )
    score = aggregate(scores)
    # Answerable questions answered with no citations → groundedness collapses.
    assert score.groundedness < 1.0
    with pytest.raises(AssertionError, match="groundedness"):
        assert_meets(score, Thresholds())
