"""Live chat-latency harness — the client-perspective TTFT / error-budget probe (#486).

The parent tracking issue (#486) closes on numbers, not vibes, but its own
*instrumentation caveat* is that AC-1 and AC-7 **cannot be verified without a
timing harness** — the repo has no metrics or tracing. This module is that
harness, built the way the repo already runs live checks (``test_eval_live.py`` +
``test_realtime_backplane_live.py``): a **doubly-gated** pytest that spends real
tokens, so it runs only on an explicit ``RUN_LIVE=1`` opt-in *and* a reachable
stack, and otherwise **skips cleanly offline** (the default ``pytest`` opens no
socket and spends nothing — issue #94 parity). It is driven by one documented
command against the running dev stack.

What it measures, from a **client's** perspective (see
:mod:`tests.eval.latency`), off the real ``realtime/`` backplane — the exact
envelope stream the WS relay forwards to a browser:

* **AC-1 — time-to-first-answer-token.** For a single-tool grounded answer on the
  FAST-tier default model, the wall-clock from the ``start`` envelope to the
  first ``delta`` carrying answer text (and the total time to ``done``), sampled
  N times and reported at p50/p95. Asserted ``< 2s`` at p50 by default.
* **AC-7 — typed terminal error against an unreachable provider.** Routes the
  answer at a black-hole ``api_base`` and measures ``start`` → ``error``,
  asserting a *typed* terminal (a Problem ``code`` + ``status``) arrives within
  the interactive budget (``≤ 30s``) — proving the ~182s retry cliff is gone.

Timing is **client-perspective**: the harness subscribes through the real
backplane *before* generation and stamps ``time.perf_counter()`` at the instant it
drains each envelope, so TTFAT / total include the Redis publish, the WS relay, and
the fan-out a browser actually pays (see :func:`tests.eval.latency.measure_client_stream`).
When a real Redis is reachable the harness measures over :class:`RedisBackplane`
(so the #487 pooled-publish path is on the clock); otherwise it falls back to
:class:`InMemoryBackplane` and says so in the printed report.

Run it with the stack up (see ``docs/runbooks/chat-latency-harness.md``)::

    docker compose up -d
    export OPENROUTER_API_KEY=sk-...
    export RUN_LIVE=1
    cd backend && uv run --extra dev pytest tests/eval/test_chat_latency_live.py -s -v

Tuning env vars (all optional): ``LATENCY_SAMPLES`` (default 5),
``LATENCY_TTFT_P50_BUDGET_SECONDS`` (2.0), ``LATENCY_ERROR_BUDGET_SECONDS`` (30.0),
``LATENCY_UNREACHABLE_BASE`` (``http://10.255.255.1:9``), ``LATENCY_ASSERT``
(``1`` to assert the budgets, ``0`` to measure-only), ``LATENCY_QUESTION``.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from urllib.parse import urlparse

import pytest
from sqlalchemy import event
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.auth.principal import Principal
from app.core.config import Settings
from app.db.base import Base
from app.db.repositories import (
    ChatSessionRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import Role
from app.llm import LLMGateway
from app.realtime.backplane import Backplane, InMemoryBackplane, RedisBackplane, is_terminal
from app.retrieval import RetrievalService
from app.search import OpenSearchStore
from app.services.chat_runtime import ChatRuntime
from app.services.provider_models import ModelRoute
from tests.eval.golden import GOLDEN_DOCUMENTS
from tests.eval.harness import seed_corpus
from tests.eval.latency import (
    StreamTiming,
    TimedEnvelope,
    measure_client_stream,
    summarise_ttft,
)

import app.db.models  # noqa: F401  isort: skip

_PG_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://lumen:lumen_local_dev@localhost:47182/lumen"
)
_OS_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:47186")


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _pg_reachable(url: str) -> bool:
    parsed = urlparse(url.replace("postgresql+asyncpg", "postgresql"))
    return _reachable(parsed.hostname or "localhost", parsed.port or 5432)


def _os_reachable(url: str) -> bool:
    parsed = urlparse(url)
    return _reachable(parsed.hostname or "localhost", parsed.port or 9200)


def _redis_reachable(url: str) -> bool:
    parsed = urlparse(url)
    return _reachable(parsed.hostname or "localhost", parsed.port or 6379)


# The harness is DOUBLY gated (issue #94 parity, mirroring test_realtime_backplane_live):
# an explicit RUN_LIVE=1 opt-in gates collection, and reachability (key + Postgres +
# OpenSearch) is probed INSIDE the `_stack_reachable` fixture — never at import — so an
# ordinary offline `pytest` neither opens a socket nor spends a token on ~5 answers.
_RUN_LIVE = os.environ.get("RUN_LIVE", "").strip() not in ("", "0", "false", "False")

# Read at import (no socket); the actual reachability probe is deferred to the fixture.
_has_key = bool(os.environ.get("OPENROUTER_API_KEY", "").strip())

# Redis is OPTIONAL: with it the harness measures the real pooled-publish path
# (#487); without it, the in-memory fan-out (the report names which was used). Its
# reachability probe lives in `_make_backplane`, not here — again, no import-time socket.
_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:47184/0")

_live = pytest.mark.skipif(
    not _RUN_LIVE,
    reason=(
        "live latency harness opted out: needs RUN_LIVE=1 (plus OPENROUTER_API_KEY + "
        f"reachable Postgres/OpenSearch) — RUN_LIVE={'set' if _RUN_LIVE else 'unset'}; "
        "skipped (offline-safe, no socket, no tokens)."
    ),
)

# --- Tunables (env-overridable so a human can measure without editing code) --

_SAMPLES = int(os.environ.get("LATENCY_SAMPLES", "5"))
_TTFT_P50_BUDGET = float(os.environ.get("LATENCY_TTFT_P50_BUDGET_SECONDS", "2.0"))
_ERROR_BUDGET = float(os.environ.get("LATENCY_ERROR_BUDGET_SECONDS", "30.0"))
_UNREACHABLE_BASE = os.environ.get("LATENCY_UNREACHABLE_BASE", "http://10.255.255.1:9")
# A single-tool grounded question answerable from the golden tax guide (so the
# real FAST model does exactly one search then answers — the AC-1 scenario).
_QUESTION = os.environ.get(
    "LATENCY_QUESTION", "What is the 2024 standard deduction for a single filer?"
)
# Assert the AC budgets by default (the DoD: no sub-issue closes on "looks
# faster"); LATENCY_ASSERT=0 turns the run into measure-only for exploration.
_ASSERT = os.environ.get("LATENCY_ASSERT", "1") != "0"

# How long to let the consumer register its subscription before the producer
# publishes `start`, so start / first-delta / terminal all arrive LIVE and their
# receipt times reflect true publish→consumer latency (the same settle the live
# backplane regression test uses). The bounded replay is the safety net if a
# sample still subscribes late, so a wiring hiccup can never drop the stream.
_SUBSCRIBE_SETTLE_SECONDS = float(os.environ.get("LATENCY_SUBSCRIBE_SETTLE_SECONDS", "0.3"))


@pytest.fixture
def _stack_reachable() -> None:
    """Skip cleanly when opted in (RUN_LIVE=1) but the key/stack isn't actually usable.

    Sockets are probed **only here** — a fixture that runs after the RUN_LIVE gate
    (``@_live``) has already let the test through — so an offline collection never
    opens one (FE-2). Mirrors ``test_realtime_backplane_live``'s fixture-side skip.
    """
    missing: list[str] = []
    if not _has_key:
        missing.append("OPENROUTER_API_KEY")
    if not _pg_reachable(_PG_URL):
        missing.append(f"Postgres@{_PG_URL}")
    if not _os_reachable(_OS_URL):
        missing.append(f"OpenSearch@{_OS_URL}")
    if missing:
        pytest.skip(
            "chat-latency harness unreachable (opted in via RUN_LIVE): " + ", ".join(missing)
        )


@dataclass(frozen=True, slots=True)
class _Stack:
    settings: Settings
    gateway: LLMGateway
    factory: async_sessionmaker[AsyncSession]
    principal: Principal
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    store: OpenSearchStore
    model: str


@asynccontextmanager
async def _seeded_stack() -> AsyncIterator[_Stack]:
    """Seed the golden corpus into an isolated schema + index (mirrors the eval).

    Real embeddings, real OpenSearch — so the grounded single-tool answer path is
    the genuine one. Schema + index are dropped in teardown so the harness never
    pollutes app data (same shape as ``test_eval_live``).
    """
    settings = Settings()  # reads real env / .env
    gateway = LLMGateway(settings)
    model = next(m.id for m in settings.chat_model_registry if m.is_default)

    engine = create_async_engine(_PG_URL)
    schema = f"lat_{uuid.uuid4().hex[:8]}"
    store = OpenSearchStore(
        base_url=_OS_URL,
        index=f"lumen-lat-{uuid.uuid4().hex[:8]}",
        dimensions=settings.llm_embedding_dimensions,
        timeout_seconds=30.0,
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(sql_text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(sql_text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(sql_text(f'SET search_path TO "{schema}", public'))
            await conn.run_sync(Base.metadata.create_all)
        await store.ensure_index()

        @event.listens_for(engine.sync_engine, "connect")
        def _set_search_path(dbapi_conn: object, _record: object) -> None:
            cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
            cur.execute(f'SET search_path TO "{schema}", public')
            cur.close()

        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as seed:
            await seed.execute(sql_text(f'SET search_path TO "{schema}", public'))
            tenant = await TenantRepository(seed).create(name="Acme")
            user = await UserRepository(seed, tenant.id).create(
                email="latency@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            await seed_corpus(
                seed,
                tenant_id=tenant.id,
                owner_id=user.id,
                documents=GOLDEN_DOCUMENTS,
                embed=gateway.embed,
                store=store,
            )
            await seed.commit()

        principal = Principal(user_id=user.id, tenant_id=tenant.id, roles=(Role.MEMBER,))
        yield _Stack(
            settings=settings,
            gateway=gateway,
            factory=factory,
            principal=principal,
            tenant_id=tenant.id,
            user_id=user.id,
            store=store,
            model=model,
        )
    finally:
        import httpx as _httpx

        async with _httpx.AsyncClient(base_url=_OS_URL, timeout=15.0) as cleanup:
            await cleanup.delete(f"/{store._index}")  # noqa: SLF001 — test teardown
            await cleanup.delete(f"/_search/pipeline/{store._index}-hybrid")  # noqa: SLF001
        await store.aclose()
        async with engine.begin() as conn:
            await conn.execute(sql_text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


def _make_backplane() -> tuple[Backplane, str]:
    """The real Redis backplane when reachable (#487 on the clock), else in-memory.

    Reachability is probed here (inside a live test, after the RUN_LIVE gate), never
    at import — so an offline collection opens no Redis socket (FE-2).
    """
    if _redis_reachable(_REDIS_URL):
        return RedisBackplane(_REDIS_URL), f"redis@{_REDIS_URL}"
    return InMemoryBackplane(), "in-memory (Redis unreachable)"


async def _capture_stream(
    backplane: Backplane, stream_id: str, events: list[TimedEnvelope]
) -> None:
    """Subscribe and stamp each envelope's monotonic CLIENT-receipt time.

    Runs as a background task started BEFORE the producer, so ``start``, the first
    answer ``delta``, and the terminal all arrive live and their
    ``time.perf_counter()`` receipt times include the publish → backplane → consumer
    path a browser pays (#486 AC-1). The generator is ``aclose``d in the ``finally``
    so the pub/sub connection (Redis) is released deterministically and nothing leaks
    under ``filterwarnings=error``. The bounded replay (both impls — the #153
    late-subscriber contract) guarantees the whole stream is seen even on a late join.
    """
    gen = backplane.subscribe(stream_id)
    try:
        async for env in gen:
            events.append(TimedEnvelope(recv=time.perf_counter(), envelope=env))
            if is_terminal(env):
                break
    finally:
        await gen.aclose()


def _build_runtime(
    stack: _Stack,
    backplane: Backplane,
    *,
    route_resolver: object | None = None,
) -> ChatRuntime:
    """A ChatRuntime wired like the chat API's ``_schedule_answer`` (the paths that matter).

    Suggestions are disabled: they are POST-terminal (#489) and out of the
    answer-time measurement (AC-5), and turning them off keeps the consumer from
    holding open for the grace window. The interactive timeout budget (#489) and
    the answer output cap (#488) are wired from Settings exactly as production
    does, so AC-7's bound is the real one.
    """
    return ChatRuntime(
        sessionmaker=stack.factory,
        gateway=stack.gateway,
        backplane=backplane,
        principal=stack.principal,
        request_id=f"latency-{uuid.uuid4().hex[:8]}",
        source_ip="127.0.0.1",
        retrieval_factory=lambda session: RetrievalService(
            session, gateway=stack.gateway, store=stack.store
        ),
        model_route_resolver=route_resolver,  # type: ignore[arg-type]
        suggestions_enabled=False,
        turn_timeout_seconds=stack.settings.llm_interactive_timeout_seconds,
        interactive_max_attempts=stack.settings.llm_interactive_max_attempts,
        answer_max_tokens=stack.settings.chat_answer_max_tokens,
        text_coalesce_chars=stack.settings.chat_text_coalesce_chars,
        text_coalesce_seconds=stack.settings.chat_text_coalesce_seconds,
    )


async def _new_session_id(stack: _Stack) -> uuid.UUID:
    async with stack.factory() as session:
        row = await ChatSessionRepository(session, stack.tenant_id).create(
            owner_id=stack.user_id, model=stack.model, title="latency"
        )
        await session.commit()
        return row.id


async def _run_once(
    stack: _Stack,
    backplane: Backplane,
    *,
    route_resolver: object | None = None,
) -> StreamTiming:
    """Produce one answer while consuming it live, and return client-perspective timing.

    The consumer subscribes through the real backplane and starts stamping receipt
    times BEFORE generation, so TTFAT / total are measured from the client side
    (FE-1), inclusive of publish + relay — not from the producer's pre-publish ``ts``.
    """
    runtime = _build_runtime(stack, backplane, route_resolver=route_resolver)
    session_id = await _new_session_id(stack)
    stream_id = uuid.uuid4().hex

    events: list[TimedEnvelope] = []
    consumer = asyncio.create_task(_capture_stream(backplane, stream_id, events))
    try:
        # Register the subscription before the producer publishes `start`.
        await asyncio.sleep(_SUBSCRIBE_SETTLE_SECONDS)
        await runtime.run(
            stream_id=stream_id,
            session_id=session_id,
            question=_QUESTION,
            model=stack.model,
            history=[],
            collection_ids=None,
        )
        # The terminal has been published; the live consumer receives it and stops.
        # Bounded so a wiring bug can never hang the harness.
        await asyncio.wait_for(consumer, timeout=90.0)
    finally:
        if not consumer.done():
            consumer.cancel()
            with suppress(asyncio.CancelledError):
                await consumer
    return measure_client_stream(events)


@_live
@pytest.mark.live
async def test_time_to_first_answer_token_p50_under_budget(_stack_reachable: None) -> None:
    """AC-1: single-tool grounded TTFT p50 < 2s on the FAST default model."""
    async with _seeded_stack() as stack:
        backplane, backplane_name = _make_backplane()
        try:
            timings: list[StreamTiming] = []
            for _ in range(_SAMPLES):
                timings.append(await _run_once(stack, backplane))
        finally:
            if isinstance(backplane, RedisBackplane):
                await backplane.aclose()

    # Every sample must be a real, terminated, non-retracted answer or the number
    # is meaningless — surface that as a failure, never a silent zero.
    for i, t in enumerate(timings):
        assert t.terminal_type == "done", f"sample {i} did not end in done: {t.terminal_type}"
        assert t.ttft_seconds is not None, f"sample {i} produced no answer delta"
        assert t.answer_text.strip(), f"sample {i} produced an empty answer"
        assert (
            t.retract_count == 0
        ), f"sample {i} retracted — not the single-tool happy path; TTFT ambiguous"

    report = summarise_ttft(timings)
    print("\n" + report.format(budget_seconds=_TTFT_P50_BUDGET, backplane=backplane_name))

    if _ASSERT:
        assert report.ttft_p50 < _TTFT_P50_BUDGET, (
            f"AC-1 FAIL: TTFT p50 {report.ttft_p50:.3f}s >= budget {_TTFT_P50_BUDGET:.3f}s "
            f"({backplane_name}, {report.samples} samples)"
        )


@_live
@pytest.mark.live
async def test_unreachable_provider_yields_typed_terminal_error_within_budget(
    _stack_reachable: None,
) -> None:
    """AC-7: an unreachable provider surfaces a typed error within the interactive budget."""

    async def _unreachable_route(_session: AsyncSession, model: str) -> ModelRoute:
        # A black-hole api_base: the connection never completes, so the whole turn
        # is bounded by the interactive asyncio budget (#489) — the ~182s cliff is
        # gone. api_key is a placeholder (never sent to a real endpoint).
        return ModelRoute(model=model, api_key="latency-unreachable", api_base=_UNREACHABLE_BASE)

    async with _seeded_stack() as stack:
        backplane, backplane_name = _make_backplane()
        try:
            timing = await _run_once(stack, backplane, route_resolver=_unreachable_route)
        finally:
            if isinstance(backplane, RedisBackplane):
                await backplane.aclose()

    budget = stack.settings.llm_interactive_timeout_seconds
    print(
        "\nchat-latency AC-7 (typed terminal error, unreachable provider)\n"
        f"  backplane            : {backplane_name}\n"
        f"  unreachable api_base : {_UNREACHABLE_BASE}\n"
        f"  interactive budget   : {budget:.1f}s (LLM_INTERACTIVE_TIMEOUT_SECONDS)\n"
        f"  terminal             : {timing.terminal_type} "
        f"code={timing.problem_code} status={timing.problem_status}\n"
        f"  time-to-error        : "
        f"{timing.total_seconds:.3f}s (budget <= {_ERROR_BUDGET:.1f}s)"
    )

    assert (
        timing.terminal_type == "error"
    ), f"expected an error terminal, got {timing.terminal_type}"
    # "Typed" = a machine-readable Problem code + status, not a bare string.
    assert timing.problem_code, "error terminal carried no typed Problem code"
    assert timing.problem_status is not None, "error terminal carried no Problem status"

    if _ASSERT:
        assert timing.total_seconds is not None, "error terminal carried no ts to measure"
        assert timing.total_seconds <= _ERROR_BUDGET, (
            f"AC-7 FAIL: time-to-typed-error {timing.total_seconds:.3f}s > budget "
            f"{_ERROR_BUDGET:.1f}s (the ~182s cliff should be gone)"
        )
