"""Live chat-latency harness — the client-perspective TTFT / error-budget probe (#486).

The parent tracking issue (#486) closes on numbers, not vibes, but its own
*instrumentation caveat* is that AC-1 and AC-7 **cannot be verified without a
timing harness** — the repo has no metrics or tracing. This module is that
harness. It is **doubly gated** (issue #94 parity, mirroring
``test_realtime_backplane_live``): it spends real tokens, so it runs only on an
explicit ``RUN_LIVE=1`` opt-in *and* a reachable stack, and otherwise **skips
cleanly offline** (a default ``pytest`` opens no socket and spends nothing).

**What path it exercises (R2-6).** Earlier revisions subscribed *directly* to the
backplane and invoked ``ChatRuntime.run`` in-process, so the "client-perspective"
claim was false: HTTP scheduling, owner binding, authentication, and the WS relay
were never on the clock, and a run could silently fall back to
``InMemoryBackplane``. This harness now drives the **real** authenticated path,
exactly as a browser does:

    POST /api/v1/chat/sessions/{id}/messages   → 202 + stream_id  (HTTP schedule,
                                                  owner binding, persistence)
    WS  /ws/chat/{stream_id}?access_token=...   → the real ``chat_ws`` relay,
                                                  consumed by an authenticated ASGI
                                                  WebSocket client (Starlette
                                                  ``TestClient.websocket_connect``,
                                                  the same client the WS API tests use)

over the process's **real** Redis :class:`RedisBackplane` (the app's own singleton;
there is **no** ``InMemoryBackplane`` fallback — a run with Redis unreachable
**skips loudly**). Timing is stamped with :func:`time.perf_counter` the instant the
WS client drains each envelope, so it includes the publish, the Redis pub/sub, and
the WS relay a browser pays. See :func:`tests.eval.latency.measure_client_stream`.

**Grounding gate (R2-6).** A direct model answer can look fast while citing
nothing, so before any AC-1 number is allowed to count, each sample must prove it
took the single-tool grounded path: a wrapping retrieval proxy asserts **exactly
one** ``search_text`` call that returned **at least one** passage, and the captured
stream must carry **at least one** ``event:citation``. A sample that answers
without grounding fails rather than contributing a misleadingly-fast TTFT.

What it measures, from a **client's** perspective:

* **AC-1 — time-to-first-answer-token.** For a single-tool grounded answer on the
  FAST-tier default model, the wall-clock from the ``start`` envelope to the first
  ``delta`` carrying answer text (and the total time to ``done``), sampled N times
  and reported at p50/p95. Asserted ``< 2s`` at p50 by default.
* **AC-7 — typed terminal error against an unreachable provider.** Routes the
  answer at a black-hole ``api_base`` and measures ``start`` → ``error``, asserting
  a *typed* terminal (a Problem ``code`` + ``status``) arrives within the
  interactive budget (``≤ 30s``) — proving the ~182s retry cliff is gone.

**What the TTFT number does and does NOT include (no overclaiming).** It DOES
include: the authenticated ``POST`` → 202 scheduling, the stream owner binding, the
answer producer, the Redis publish + pub/sub, the ``chat_ws`` relay, and the WS
frame delivery to an authenticated client. It does NOT include a real browser's
network RTT / TLS (the ASGI client is in-process) nor React render. One transport
caveat: a browser — and this client — connect the WS **after** the 202, so
envelopes already buffered when the socket attaches are drained from the #153
replay at connect time; for a fast answer the inter-token *pacing* therefore
reflects replay drain rather than live per-token arrival. TTFAT stays meaningful
because a grounded answer spends seconds in retrieval + generation *after* the
socket has attached, so the first answer ``delta`` is delivered live; only its
``start`` anchor is the connect-time replay of the already-published ``start``,
which if anything slightly *under*-counts TTFAT (a later start anchor), never
inflates it.

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
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import app.api.v1.chat as chat_module
import app.main as main_module
import app.search.store as store_module
from app.api.deps import get_backplane, get_db_session, get_llm_gateway
from app.auth import hash_password
from app.core.config import Settings
from app.db.base import Base
from app.db.repositories import TenantRepository, UserRepository
from app.domain.entities import Role
from app.domain.retrieval import RetrievedPassage
from app.llm import LLMGateway
from app.main import create_app
from app.realtime.backplane import is_terminal
from app.retrieval import RetrievalService
from app.search import OpenSearchStore
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

_PASSWORD = "latency-dev-password"


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


# The opt-in gate is STRICT (R2-5): ONLY the exact string "1" enables the live
# harness. This host has an OPENROUTER_API_KEY, so a lenient truthy parse (which
# accepted "true"/"2"/… and even mis-parsed "no") would spend real tokens on an
# unintended value. "1" and nothing else — matching the documented opt-in. The
# reachability probe is deferred to the fixture, so an offline import opens no
# socket (FE-2); this is a pure env read.
_RUN_LIVE = os.environ.get("RUN_LIVE") == "1"

# Read at import (no socket); the actual reachability probe is deferred to the fixture.
_has_key = bool(os.environ.get("OPENROUTER_API_KEY", "").strip())

_live = pytest.mark.skipif(
    not _RUN_LIVE,
    reason=(
        "live latency harness opted out: needs RUN_LIVE=1 exactly (plus "
        "OPENROUTER_API_KEY + reachable Postgres/OpenSearch/Redis) — "
        f"RUN_LIVE={'=1' if _RUN_LIVE else 'not 1'}; skipped (offline-safe, no socket, "
        "no tokens)."
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

# A generous upper bound on envelopes drained from one answer stream before we
# give up waiting for a terminal (coalescing bounds the delta count; a hung
# producer is separately bounded by the interactive budget for AC-7). Purely a
# safety valve so a wiring bug can never spin forever.
_MAX_ENVELOPES = 5000


# --- Reachability gate (Redis now REQUIRED — R2-6) --------------------------


@pytest.fixture
def _require_stack() -> Settings:
    """Skip cleanly when opted in (RUN_LIVE=1) but the key/stack isn't usable.

    Sockets are probed **only here** — after the ``RUN_LIVE`` gate (``@_live``)
    has already let the test through — so an offline collection never opens one
    (FE-2). Redis is **required** now (R2-6): the harness drives the app's real
    ``RedisBackplane`` end-to-end and must never silently measure an in-memory
    fan-out, so an unreachable Redis is a loud skip, not a fallback. Returns the
    validated ``Settings`` (the app reads the same env) so the tests can name the
    real ``redis_url`` in their report.
    """
    try:
        settings = Settings()  # validates the whole stack config (DB/Redis/S3/…)
    except Exception as exc:  # noqa: BLE001 — incomplete env → skip, not error
        pytest.skip(f"Settings unavailable for the live latency harness: {type(exc).__name__}")
    missing: list[str] = []
    if not _has_key:
        missing.append("OPENROUTER_API_KEY")
    if not _pg_reachable(_PG_URL):
        missing.append(f"Postgres@{_PG_URL}")
    if not _os_reachable(_OS_URL):
        missing.append(f"OpenSearch@{_OS_URL}")
    if not _redis_reachable(settings.redis_url):
        missing.append(f"Redis@{settings.redis_url}")
    if missing:
        pytest.skip(
            "chat-latency harness unreachable (opted in via RUN_LIVE=1): " + ", ".join(missing)
        )
    return settings


# --- Seeding: an isolated schema + index the real app is pointed at ---------


@dataclass(frozen=True, slots=True)
class _SeedInfo:
    schema: str
    index: str
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    email: str


def _attach_search_path(engine: object, schema: str) -> None:
    """Pin every new connection on ``engine`` to the isolated ``schema``."""

    @event.listens_for(engine.sync_engine, "connect")  # type: ignore[attr-defined]
    def _set_search_path(dbapi_conn: object, _record: object) -> None:
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cur.execute(f'SET search_path TO "{schema}", public')
        cur.close()


async def _seed() -> _SeedInfo:
    """Seed the golden corpus into a throwaway schema + index (mirrors the eval).

    Real embeddings, real OpenSearch — so the grounded single-tool answer path the
    app later drives is the genuine one. Runs on its OWN event loop (the caller
    uses ``asyncio.run``): the engine/store/gateway created here are closed here
    and never reused on the app's serving loop (#140). The rows/docs it commits are
    durable in Postgres/OpenSearch, so the app (a different loop) reads them back.
    """
    settings = Settings()
    gateway = LLMGateway(settings)
    schema = f"lat_{uuid.uuid4().hex[:8]}"
    index = f"lumen-lat-{uuid.uuid4().hex[:8]}"

    engine = create_async_engine(_PG_URL)
    store = OpenSearchStore(
        base_url=_OS_URL,
        index=index,
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

        _attach_search_path(engine, schema)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as seed:
            await seed.execute(sql_text(f'SET search_path TO "{schema}", public'))
            tenant = await TenantRepository(seed).create(name="Acme")
            user = await UserRepository(seed, tenant.id).create(
                email="latency@acme.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
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
        return _SeedInfo(
            schema=schema,
            index=index,
            tenant_id=tenant.id,
            user_id=user.id,
            email="latency@acme.test",
        )
    finally:
        await store.aclose()
        await engine.dispose()


async def _teardown(info: _SeedInfo, app_engine: object) -> None:
    """Drop the seeded schema + index; dispose the app engine (own loop)."""
    import httpx as _httpx

    async with _httpx.AsyncClient(base_url=_OS_URL, timeout=15.0) as cleanup:
        await cleanup.delete(f"/{info.index}")
        await cleanup.delete(f"/_search/pipeline/{info.index}-hybrid")
    # The app engine uses NullPool, so it holds no checked-in connections here —
    # ``dispose`` just releases the (empty) pool and is loop-safe to call from
    # this teardown loop even though requests ran on the app's serving loop.
    await app_engine.dispose()  # type: ignore[attr-defined]
    engine = create_async_engine(_PG_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(sql_text(f'DROP SCHEMA IF EXISTS "{info.schema}" CASCADE'))
    finally:
        await engine.dispose()


# --- Retrieval instrumentation (the grounding gate, R2-6) -------------------


@dataclass
class _RetrievalCounter:
    """Shared across every per-answer retrieval proxy in one harness."""

    calls: int = 0
    nonempty: int = 0


class _CountingRetrieval:
    """Wrap a real :class:`RetrievalService`, counting grounded ``search_text`` calls.

    Only ``search_text`` is instrumented (the single-tool grounded path AC-1
    asserts); every other method is forwarded verbatim, so the wrapped service
    behaves identically otherwise. The counter is shared, so the harness can assert
    a per-sample delta of *exactly one* call that returned at least one passage.
    """

    def __init__(self, inner: RetrievalService, counter: _RetrievalCounter) -> None:
        self._inner = inner
        self._counter = counter

    async def search_text(self, **kwargs: object) -> list[RetrievedPassage]:
        self._counter.calls += 1
        passages = await self._inner.search_text(**kwargs)  # type: ignore[arg-type]
        if passages:
            self._counter.nonempty += 1
        return passages

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


async def _unreachable_route(_session: AsyncSession, model: str) -> ModelRoute:
    # A black-hole api_base: the connection never completes, so the whole turn is
    # bounded by the interactive asyncio budget (#489) — the ~182s cliff is gone.
    # api_key is a placeholder (never sent to a real endpoint).
    return ModelRoute(model=model, api_key="latency-unreachable", api_base=_UNREACHABLE_BASE)


def _wrap_runtime(real_cls: type, *, counter: _RetrievalCounter, unreachable_route: bool) -> object:
    """A ``ChatRuntime`` factory the router builds through, with test seams injected.

    The router's ``_schedule_answer`` constructs ``ChatRuntime(**kwargs)``; this
    factory intercepts that to (1) wrap retrieval in the counting proxy over the
    router's OWN gateway (so query embeddings are made on the app's serving loop,
    never the seed loop — #140), (2) disable follow-up suggestions so the WS closes
    on the ``done`` terminal (answer-time only, AC-5), and (3) for AC-7 only, route
    the answer at the black-hole base. Everything else is production wiring.
    """

    def _factory(**kwargs: object) -> object:
        gateway = kwargs["gateway"]
        kwargs["retrieval_factory"] = lambda session: _CountingRetrieval(
            RetrievalService(session, gateway=gateway),  # type: ignore[arg-type]
            counter,
        )
        kwargs["suggestions_enabled"] = False
        if unreachable_route:
            kwargs["model_route_resolver"] = _unreachable_route
        return real_cls(**kwargs)

    return _factory


class _NoopStore:
    """Stub the object store so the app lifespan's ``ensure_bucket`` opens no socket.

    The answer path never touches object storage (retrieval + citations only), so
    this is inert for the measurement and keeps startup network- and warning-free.
    """

    async def ensure_bucket(self) -> None:
        return None


# --- The driven harness (real HTTP send + real WS relay) --------------------


@dataclass
class _Harness:
    client: TestClient
    token: str
    counter: _RetrievalCounter
    settings: Settings
    grounded: bool = True


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_session_override(
    factory: async_sessionmaker[AsyncSession],
) -> Callable[[], AsyncIterator[AsyncSession]]:
    async def _override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    return _override


@contextmanager
def _ws_harness(settings: Settings, *, unreachable_route: bool) -> Iterator[_Harness]:
    """Bring up the REAL app pointed at the seeded schema/index over real Redis.

    Seeding + teardown run on their own loops (``asyncio.run``); the app runs on the
    sync ``TestClient``'s portal loop, where its DB engine (NullPool, so no
    connection straddles a loop), its OpenSearch store, its Redis backplane, and its
    gateway are all created and used — the production single-loop invariant (#140).
    The DB session dependency and the runtime's sessionmaker are pointed at the
    isolated schema; the app's shared OpenSearch store is pointed at the seeded
    index (and so is closed by the app lifespan on the serving loop); the backplane
    is the app's own real ``RedisBackplane`` (no override, no in-memory fallback).
    """
    info = asyncio.run(_seed())
    counter = _RetrievalCounter()

    # Rebuild the app singletons fresh for THIS harness: AC-1 and AC-7 each run on
    # their own ``TestClient`` portal loop, so a gateway/backplane cached on the
    # prior harness's (now-dead) loop must never be reused (#140). The backplane is
    # already loop-keyed and re-creates its client per loop; clearing the caches
    # keeps the two runs fully independent and each singleton built on its own loop.
    get_llm_gateway.cache_clear()
    get_backplane.cache_clear()

    app_store = OpenSearchStore(
        base_url=_OS_URL,
        index=info.index,
        dimensions=settings.llm_embedding_dimensions,
        timeout_seconds=30.0,
    )
    app_engine = create_async_engine(_PG_URL, poolclass=NullPool)
    _attach_search_path(app_engine, info.schema)
    app_factory = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    application = create_app()
    try:
        with ExitStack() as stack:
            # Point the app-process OpenSearch store at the seeded index; the app
            # lifespan's ``aclose_search_store`` then closes THIS store on the
            # serving loop, so no client leaks under ``filterwarnings=error``.
            stack.enter_context(patch.object(store_module, "_app_store", app_store))
            # The runtime's own session (built off the request cycle) → seeded schema.
            stack.enter_context(patch.object(chat_module, "get_sessionmaker", lambda: app_factory))
            # The router builds ChatRuntime through this factory (retrieval proxy +
            # suggestions off + optional black-hole route).
            stack.enter_context(
                patch.object(
                    chat_module,
                    "ChatRuntime",
                    _wrap_runtime(
                        chat_module.ChatRuntime,  # type: ignore[attr-defined]
                        counter=counter,
                        unreachable_route=unreachable_route,
                    ),
                )
            )
            stack.enter_context(patch.object(main_module, "get_object_store", lambda: _NoopStore()))
            # The request-path DB session → seeded schema.
            application.dependency_overrides[get_db_session] = _make_session_override(app_factory)

            client = stack.enter_context(TestClient(application))
            token = _http_login(client, info.email)
            yield _Harness(client=client, token=token, counter=counter, settings=settings)
    finally:
        application.dependency_overrides.clear()
        asyncio.run(_teardown(info, app_engine))


def _http_login(client: TestClient, email: str) -> str:
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


def _count_citations(events: list[TimedEnvelope]) -> int:
    return sum(
        1
        for e in events
        if e.envelope.get("type") == "event" and e.envelope.get("name") == "citation"
    )


def _run_answer_ws(h: _Harness) -> StreamTiming:
    """Schedule one answer over HTTP (202) and consume it over the real WS relay.

    This is the whole client path: create a fresh session, POST the message
    (persist + owner-bind + schedule), then connect the authenticated WS and stamp
    the receipt time of every envelope until the terminal. For a grounded run it
    enforces the grounding gate BEFORE returning a countable timing: exactly one
    retrieval call that returned passages, and at least one ``event:citation`` on
    the wire.
    """
    client = h.client
    sid = client.post(
        "/api/v1/chat/sessions", headers=_auth(h.token), json={"title": "latency"}
    ).json()["id"]

    calls_before, nonempty_before = h.counter.calls, h.counter.nonempty
    resp = client.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        headers=_auth(h.token),
        json={"content": _QUESTION},
    )
    assert resp.status_code == 202, resp.text
    stream_id = resp.json()["stream_id"]

    events: list[TimedEnvelope] = []
    with client.websocket_connect(f"/ws/chat/{stream_id}?access_token={h.token}") as ws:
        for _ in range(_MAX_ENVELOPES):
            env = ws.receive_json()
            events.append(TimedEnvelope(recv=time.perf_counter(), envelope=env))
            if is_terminal(env):
                break

    timing = measure_client_stream(events)

    if h.grounded:
        retrieval_calls = h.counter.calls - calls_before
        grounded_calls = h.counter.nonempty - nonempty_before
        citations = _count_citations(events)
        assert retrieval_calls == 1, (
            f"expected EXACTLY one retrieval call on the single-tool path, got "
            f"{retrieval_calls} — TTFT would be measured off the wrong path"
        )
        assert grounded_calls == 1, (
            "the retrieval call returned no passages — a direct/ungrounded answer "
            "must not count toward the single-tool grounded budget"
        )
        assert citations >= 1, (
            "no grounded citation was streamed — the answer was not grounded in "
            "retrieval, so its TTFT must not count"
        )
    return timing


# --- AC-1 -------------------------------------------------------------------


@_live
@pytest.mark.live
def test_time_to_first_answer_token_p50_under_budget(_require_stack: Settings) -> None:
    """AC-1: single-tool grounded TTFT p50 < 2s on the FAST default model.

    Driven end-to-end over the authenticated HTTP send → real ``chat_ws`` WS relay
    → real Redis backplane; each sample must pass the grounding gate (one retrieval
    call + a citation) before its TTFT counts.
    """
    with _ws_harness(_require_stack, unreachable_route=False) as h:
        timings: list[StreamTiming] = [_run_answer_ws(h) for _ in range(_SAMPLES)]

    # Every sample must be a real, terminated, non-retracted answer or the number
    # is meaningless — surface that as a failure, never a silent zero.
    for i, t in enumerate(timings):
        assert t.terminal_type == "done", f"sample {i} did not end in done: {t.terminal_type}"
        assert t.ttft_seconds is not None, f"sample {i} produced no answer delta"
        assert t.answer_text.strip(), f"sample {i} produced an empty answer"
        assert (
            t.retract_count == 0
        ), f"sample {i} retracted — not the single-tool happy path; TTFT ambiguous"

    backplane_name = f"redis@{_require_stack.redis_url}"
    report = summarise_ttft(timings)
    print("\n" + report.format(budget_seconds=_TTFT_P50_BUDGET, backplane=backplane_name))

    if _ASSERT:
        assert report.ttft_p50 < _TTFT_P50_BUDGET, (
            f"AC-1 FAIL: TTFT p50 {report.ttft_p50:.3f}s >= budget {_TTFT_P50_BUDGET:.3f}s "
            f"({backplane_name}, {report.samples} samples)"
        )


# --- AC-7 -------------------------------------------------------------------


@_live
@pytest.mark.live
def test_unreachable_provider_yields_typed_terminal_error_within_budget(
    _require_stack: Settings,
) -> None:
    """AC-7: an unreachable provider surfaces a typed error within the interactive budget.

    Same real path (authenticated 202 → ``chat_ws`` → Redis), but the answer is
    routed at a black-hole ``api_base``, so the turn is bounded by the interactive
    asyncio budget and terminates in a typed ``error`` (no grounding expected —
    the model call never connects, so no retrieval/citation).
    """
    with _ws_harness(_require_stack, unreachable_route=True) as h:
        h.grounded = False
        timing = _run_answer_ws(h)

    budget = _require_stack.llm_interactive_timeout_seconds
    print(
        "\nchat-latency AC-7 (typed terminal error, unreachable provider)\n"
        f"  backplane            : redis@{_require_stack.redis_url}\n"
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
