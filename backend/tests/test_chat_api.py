"""Chat API + WS tests — the /chat contract, the 202 send, the answer stream.

End-to-end against the real FastAPI app over an offline in-memory SQLite DB and an
in-memory backplane (no Postgres / Redis / model), mirroring the collections API
test. Covers:

* sessions CRUD happy paths + the contract shapes;
* the 202 send → ``SendMessageResponse`` (persisted user message + stream_id),
  the assistant answer then reloading via ``GET .../messages`` with citations
  (CC-11);
* the WS answer stream: auth gate (INV-4), relayed envelope sequence, exactly-one
  terminal, isolation by stream_id;
* negatives: unknown model → 422 (INV-8); cross-tenant / other-owner session →
  404 (INV-1/INV-2); no/bad token → 401 (INV-4).

The answer runtime is driven by a scripted fake gateway + fake retrieval wired in
via monkeypatch, so the full REST→persist→stream path runs without a live model.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

import app.api.v1.chat as chat_module
from app.api.deps import get_backplane_dep, get_db_session
from app.auth import hash_password
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    LlmProviderRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import LlmProviderStatus, Role, SecretKind
from app.domain.llm import StreamEvent, ToolCall
from app.domain.retrieval import DocumentMatch, DocumentText, RetrievedPassage
from app.main import _drain_answer_tasks, create_app, lifespan
from app.realtime.backplane import InMemoryBackplane, StreamOwner
from app.services.audit import AuditSink
from app.services.provider_models import make_provider_model_id
from app.services.secrets_service import build_secrets_service

import app.db.models  # noqa: F401  isort: skip

_PASSWORD = "devpassword"


class _Seeded:
    def __init__(
        self,
        *,
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        alice_id: uuid.UUID,
        bob_id: uuid.UUID,
        carol_id: uuid.UUID,
        alice_doc: uuid.UUID,
        alice_chunk: uuid.UUID,
    ) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice_id = alice_id
        self.bob_id = bob_id
        self.carol_id = carol_id
        self.alice_email = "alice@acme.test"
        self.bob_email = "bob@acme.test"
        self.carol_email = "carol@globex.test"
        self.alice_doc = alice_doc
        self.alice_chunk = alice_chunk


# --- Scripted fakes for the answer runtime ---------------------------------


class _ScriptedGateway:
    def __init__(self, passage_chunk_id: uuid.UUID) -> None:
        self._chunk_id = passage_chunk_id

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
        # Turn shape is decided by whether the transcript already has a tool turn:
        # a real gateway streams; here we infer the turn from message count.
        msgs = list(messages)  # type: ignore[arg-type]
        has_tool_result = any(getattr(m, "role", None).value == "tool" for m in msgs)
        if not has_tool_result:
            yield StreamEvent(
                tool_calls=(
                    ToolCall(id="c1", name="search_text", arguments={"query": "deduction"}),
                ),
                finish_reason="tool_calls",
            )
        else:
            yield StreamEvent(text="The 2024 standard deduction is $14,600.")
            yield StreamEvent(finish_reason="stop")


class _CapturingGateway:
    """A gateway that records the credential kwargs of each ``stream_tools`` call.

    Answers in a single tool-free turn (so the runtime loop makes exactly one
    gateway call) and captures ``(model, api_key, api_base)`` — the routing seam
    PR 2a threads down. ``calls[0]`` is the first (and only) turn's kwargs.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

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
        self.calls.append({"model": model, "api_key": api_key, "api_base": api_base})
        yield StreamEvent(text="Routed answer.")
        yield StreamEvent(finish_reason="stop")


class _FakeRetrieval:
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


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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
            ta = await TenantRepository(seed).create(name="Acme")
            tb = await TenantRepository(seed).create(name="Globex")
            alice = await UserRepository(seed, ta.id).create(
                email="alice@acme.test", password_hash=hash_password(_PASSWORD), roles=[Role.MEMBER]
            )
            bob = await UserRepository(seed, ta.id).create(
                email="bob@acme.test", password_hash=hash_password(_PASSWORD), roles=[Role.MEMBER]
            )
            carol = await UserRepository(seed, tb.id).create(
                email="carol@globex.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
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
                [ChunkInput(text="2024 standard deduction is $14,600.", char_start=0, char_end=34)],
            )
            await seed.commit()
            factory.lumen_seeded = _Seeded(  # type: ignore[attr-defined]
                tenant_a=ta.id,
                tenant_b=tb.id,
                alice_id=alice.id,
                bob_id=bob.id,
                carol_id=carol.id,
                alice_doc=doc.id,
                alice_chunk=chunks[0].id,
            )
            yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> _Seeded:
    return sessionmaker.lumen_seeded  # type: ignore[attr-defined, no-any-return]


@pytest.fixture
def backplane() -> InMemoryBackplane:
    return InMemoryBackplane()


@pytest.fixture
def app(
    sessionmaker: async_sessionmaker[AsyncSession],
    backplane: InMemoryBackplane,
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FastAPI]:
    application = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_db_session] = _override_session
    application.dependency_overrides[get_backplane_dep] = lambda: backplane

    # The runtime is built inside the router with module-level singletons; point
    # them at the test sessionmaker + scripted gateway + fake retrieval so the
    # answer path runs offline. The WS endpoint pulls the backplane via
    # get_backplane(); override that too.
    passage = RetrievedPassage(
        chunk_id=seeded.alice_chunk,
        document_id=seeded.alice_doc,
        document_name="taxes.pdf",
        ord=0,
        text="2024 standard deduction is $14,600.",
        char_start=0,
        char_end=34,
        score=0.9,
    )
    gateway = _ScriptedGateway(seeded.alice_chunk)
    retrieval = _FakeRetrieval(passage)

    monkeypatch.setattr(chat_module, "get_sessionmaker", lambda: sessionmaker)
    monkeypatch.setattr(chat_module, "get_llm_gateway", lambda: gateway)
    # Inject the fake retrieval factory + the test backplane into the runtime.
    real_runtime_cls = chat_module.ChatRuntime

    def _runtime_factory(**kwargs: object) -> object:
        kwargs["retrieval_factory"] = lambda _session: retrieval
        kwargs["backplane"] = backplane
        return real_runtime_cls(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(chat_module, "ChatRuntime", _runtime_factory)
    import app.realtime.chat_ws as ws_module

    monkeypatch.setattr(ws_module, "get_backplane", lambda: backplane)

    # The sync TestClient runs the app lifespan, which best-effort calls
    # ``ensure_bucket`` (MinIO). Stub the object store so startup never reaches
    # the network (mirrors test_health_api). Harmless for the async httpx client
    # (ASGITransport skips lifespan).
    import app.main as main_module

    class _NoopStore:
        async def ensure_bucket(self) -> None:
            return None

    monkeypatch.setattr(main_module, "get_object_store", lambda: _NoopStore())

    yield application
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    # The ASGITransport client skips the app lifespan, so the lifespan's
    # answer-task drain (#156) never runs; cancel any detached producer still in
    # flight here so it doesn't outlive this loop and leak (mirrors the lifespan
    # teardown). The runtime turns CancelledError into its terminal envelope.
    tasks: set[asyncio.Task[None]] = app.state.answer_tasks
    for task in list(tasks):
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _login(client: AsyncClient, email: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- Sessions CRUD ----------------------------------------------------------


async def test_create_and_get_session(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post("/api/v1/chat/sessions", headers=_auth(token), json={"title": "Q3"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) >= {"id", "title", "model", "owner_id", "message_count", "created_at"}
    assert body["message_count"] == 0
    got = await client.get(f"/api/v1/chat/sessions/{body['id']}", headers=_auth(token))
    assert got.status_code == 200
    assert got.json()["id"] == body["id"]


async def test_create_session_unknown_model_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/chat/sessions", headers=_auth(token), json={"model": "bogus/model"}
    )
    assert resp.status_code == 422


async def test_patch_and_delete_session(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await client.post("/api/v1/chat/sessions", headers=_auth(token), json={"title": "a"})
    sid = created.json()["id"]
    patched = await client.patch(
        f"/api/v1/chat/sessions/{sid}", headers=_auth(token), json={"title": "b"}
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "b"
    deleted = await client.delete(f"/api/v1/chat/sessions/{sid}", headers=_auth(token))
    assert deleted.status_code == 204
    gone = await client.get(f"/api/v1/chat/sessions/{sid}", headers=_auth(token))
    assert gone.status_code == 404


async def test_patch_empty_body_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await client.post("/api/v1/chat/sessions", headers=_auth(token), json={"title": "a"})
    sid = created.json()["id"]
    resp = await client.patch(f"/api/v1/chat/sessions/{sid}", headers=_auth(token), json={})
    assert resp.status_code == 422


# --- Negatives: tenancy / ownership / auth ---------------------------------


async def test_get_cross_tenant_session_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    carol = await _login(client, seeded.carol_email)
    created = await client.post("/api/v1/chat/sessions", headers=_auth(carol), json={"title": "c"})
    sid = created.json()["id"]
    alice = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/chat/sessions/{sid}", headers=_auth(alice))
    assert resp.status_code == 404


async def test_get_other_owner_session_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    bob = await _login(client, seeded.bob_email)
    created = await client.post("/api/v1/chat/sessions", headers=_auth(bob), json={"title": "b"})
    sid = created.json()["id"]
    alice = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/chat/sessions/{sid}", headers=_auth(alice))
    assert resp.status_code == 404


async def test_list_sessions_without_token_is_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/chat/sessions")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")


async def test_send_to_unknown_session_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        f"/api/v1/chat/sessions/{uuid.uuid4()}/messages",
        headers=_auth(token),
        json={"content": "hi"},
    )
    assert resp.status_code == 404


async def test_send_unknown_model_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await client.post("/api/v1/chat/sessions", headers=_auth(token), json={"title": "s"})
    sid = created.json()["id"]
    resp = await client.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        headers=_auth(token),
        json={"content": "hi", "model": "nope/model"},
    )
    assert resp.status_code == 422


async def test_send_empty_content_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await client.post("/api/v1/chat/sessions", headers=_auth(token), json={"title": "s"})
    sid = created.json()["id"]
    resp = await client.post(
        f"/api/v1/chat/sessions/{sid}/messages", headers=_auth(token), json={"content": ""}
    )
    assert resp.status_code == 422


# --- 202 send: returns the user message + stream_id ------------------------


async def test_send_returns_202_with_user_message_and_stream_id(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.alice_email)
    created = await client.post("/api/v1/chat/sessions", headers=_auth(token), json={"title": "s"})
    sid = created.json()["id"]
    resp = await client.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        headers=_auth(token),
        json={"content": "What is the 2024 standard deduction?"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["message"]["role"] == "user"
    assert body["message"]["content"] == "What is the 2024 standard deduction?"
    assert isinstance(body["stream_id"], str) and body["stream_id"]


# --- WS streaming -----------------------------------------------------------


def test_ws_rejects_missing_token(app: FastAPI) -> None:
    with TestClient(app) as client:
        with pytest.raises(Exception):  # noqa: B017 — starlette raises on policy close
            with client.websocket_connect("/ws/chat/some-stream"):
                pass


def test_ws_missing_token_denied_before_accept_no_envelope(app: FastAPI) -> None:
    """A missing token is denied **before accept** — handshake rejected, no leak.

    Pins the documented client-visible outcome (issue #158): the endpoint closes
    the socket before ``accept``. Over a real transport that surfaces as an HTTP
    403 rejection of the WebSocket upgrade; the in-process ``TestClient``
    short-circuits the close into a ``WebSocketDisconnect`` (it never reaches the
    HTTP-403 translation a network client sees). Either way the socket is never
    accepted and **no envelope** is delivered — so this asserts the disconnect is
    raised and nothing was received, keeping the corrected docstrings honest.
    """
    with TestClient(app) as client:
        leaked: dict[str, object] | None = None
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/chat/some-stream") as ws:
                # Reaching the body means the socket was accepted — any receive is
                # a leak. The denial path must raise before yielding an envelope.
                leaked = ws.receive_json()
        assert leaked is None, f"envelope leaked on a pre-accept denial: {leaked!r}"


def _fill_replay(backplane: InMemoryBackplane, envs: list[dict[str, object]]) -> None:
    """Synchronously seed the in-memory backplane's replay with ``envs``.

    Publishing only appends to the replay buffer (no subscriber exists yet), so it
    is safe to run on a throwaway loop; the WS consumer then replays it. Note: the
    WS tests are **sync** (``TestClient`` runs its own portal loop) — calling the
    sync WS client from inside an ``async def`` test would deadlock, so these are
    deliberately not async.
    """

    async def _run() -> None:
        for env in envs:
            await backplane.publish(str(env["streamId"]), env)

    asyncio.run(_run())


def _bind_owner(
    backplane: InMemoryBackplane, stream_id: str, *, owner_id: uuid.UUID, tenant_id: uuid.UUID
) -> None:
    """Synchronously bind a stream's owner (the 202 does this in real flow).

    The replay-based WS tests publish envelopes directly without going through the
    202 send, so they must seed the owner binding too — the consumer now refuses
    to relay a stream that is not bound to the connecting principal (INV-1/INV-2).
    """

    async def _run() -> None:
        await backplane.bind_owner(stream_id, StreamOwner(owner_id=owner_id, tenant_id=tenant_id))

    asyncio.run(_run())


def test_ws_relays_published_envelopes(
    app: FastAPI, backplane: InMemoryBackplane, seeded: _Seeded
) -> None:
    # Publish a full lifecycle first; the backplane's replay buffer lets the WS
    # consumer (connecting after) relay it and stop on the terminal — the
    # realistic "connect right after the 202" flow.
    from app.realtime import envelopes

    stream_id = "ws-test-1"
    _bind_owner(backplane, stream_id, owner_id=seeded.alice_id, tenant_id=seeded.tenant_a)
    _fill_replay(
        backplane,
        [
            envelopes.start(stream_id, 0, data={"model": "m"}),
            envelopes.delta(stream_id, 1, {"text": "hi"}),
            envelopes.done(stream_id, 2, data={"citationCount": 0}),
        ],
    )

    with TestClient(app) as client:
        token = client.post(
            "/api/v1/auth/login", json={"email": "alice@acme.test", "password": _PASSWORD}
        ).json()["access_token"]
        with client.websocket_connect(f"/ws/chat/{stream_id}?access_token={token}") as ws:
            received = [ws.receive_json(), ws.receive_json(), ws.receive_json()]

    assert [e["type"] for e in received] == ["start", "delta", "done"]
    assert received[-1]["data"]["citationCount"] == 0


def test_ws_terminal_error_is_terminal(
    app: FastAPI, backplane: InMemoryBackplane, seeded: _Seeded
) -> None:
    from app.realtime import envelopes

    stream_id = "ws-test-err"
    _bind_owner(backplane, stream_id, owner_id=seeded.alice_id, tenant_id=seeded.tenant_a)
    _fill_replay(
        backplane,
        [
            envelopes.start(stream_id, 0),
            envelopes.error(stream_id, 1, {"title": "Boom", "status": 503, "code": "x"}),
        ],
    )

    with TestClient(app) as client:
        token = client.post(
            "/api/v1/auth/login", json={"email": "alice@acme.test", "password": _PASSWORD}
        ).json()["access_token"]
        with client.websocket_connect(f"/ws/chat/{stream_id}?access_token={token}") as ws:
            start = ws.receive_json()
            err = ws.receive_json()

    assert start["type"] == "start"
    assert err["type"] == "error"
    assert err["problem"]["status"] == 503


def _assert_ws_denied_no_leak(client: TestClient, stream_id: str, token: str) -> None:
    """Connect to ``stream_id`` and assert the consumer denied it with no leak.

    A denied stream closes **before accept**, so the ``TestClient`` raises
    ``WebSocketDisconnect`` on connect. The leak we guard against is the opposite:
    the server accepts and relays an envelope — so if any ``receive_json``
    succeeds, that envelope leaked and the test must fail. ``WebSocketDisconnect``
    is *not* an ``AssertionError``, so a real leak (our explicit failure) is never
    swallowed by the disconnect handling.
    """
    leaked: dict[str, object] | None = None
    try:
        with client.websocket_connect(f"/ws/chat/{stream_id}?access_token={token}") as ws:
            # Reaching here means the socket was accepted; any envelope received is
            # a cross-user leak of the answer stream.
            leaked = ws.receive_json()
    except WebSocketDisconnect:
        pass  # Closed before/without delivering anything — the correct deny path.
    assert leaked is None, f"answer-stream envelope leaked to a foreign subscriber: {leaked!r}"


def test_ws_denies_same_tenant_other_owner(
    app: FastAPI, backplane: InMemoryBackplane, seeded: _Seeded
) -> None:
    # Alice's answer stream (bound to alice at the 202) carries permitted-only
    # citation snippets. A *second* authenticated user in the same tenant (bob)
    # who learned the stream_id must be denied — the socket closes (policy code)
    # and NO envelope leaks (INV-1/INV-2).
    from app.realtime import envelopes

    stream_id = "alice-stream-bob-attacks"
    _bind_owner(backplane, stream_id, owner_id=seeded.alice_id, tenant_id=seeded.tenant_a)
    _fill_replay(
        backplane,
        [
            envelopes.start(stream_id, 0, data={"model": "m"}),
            envelopes.delta(stream_id, 1, {"text": "secret answer for alice"}),
            envelopes.done(stream_id, 2, data={"citationCount": 1}),
        ],
    )

    with TestClient(app) as client:
        bob_token = client.post(
            "/api/v1/auth/login", json={"email": seeded.bob_email, "password": _PASSWORD}
        ).json()["access_token"]
        _assert_ws_denied_no_leak(client, stream_id, bob_token)


def test_ws_denies_cross_tenant_subscriber(
    app: FastAPI, backplane: InMemoryBackplane, seeded: _Seeded
) -> None:
    # A cross-tenant authenticated user (carol, tenant B) connecting to alice's
    # (tenant A) stream_id is denied identically — closed, no envelope (INV-1).
    from app.realtime import envelopes

    stream_id = "alice-stream-carol-attacks"
    _bind_owner(backplane, stream_id, owner_id=seeded.alice_id, tenant_id=seeded.tenant_a)
    _fill_replay(
        backplane,
        [
            envelopes.start(stream_id, 0, data={"model": "m"}),
            envelopes.delta(stream_id, 1, {"text": "secret answer for alice"}),
            envelopes.done(stream_id, 2, data={"citationCount": 1}),
        ],
    )

    with TestClient(app) as client:
        carol_token = client.post(
            "/api/v1/auth/login", json={"email": seeded.carol_email, "password": _PASSWORD}
        ).json()["access_token"]
        _assert_ws_denied_no_leak(client, stream_id, carol_token)


def test_send_then_stream_then_history_reloads_citations(app: FastAPI, seeded: _Seeded) -> None:
    # The integration headline (all on the sync TestClient's one loop): POST send
    # (202) runs the scripted answer runtime as a background task — which streams
    # to the backplane and persists the assistant message + citation — then the WS
    # relays the lifecycle and GET .../messages reloads the assistant turn WITH
    # its citation (CC-11).
    with TestClient(app) as client:
        token = client.post(
            "/api/v1/auth/login", json={"email": seeded.alice_email, "password": _PASSWORD}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        sid = client.post("/api/v1/chat/sessions", headers=headers, json={"title": "s"}).json()[
            "id"
        ]
        sent = client.post(
            f"/api/v1/chat/sessions/{sid}/messages",
            headers=headers,
            json={"content": "What is the 2024 standard deduction?"},
        )
        assert sent.status_code == 202, sent.text
        stream_id = sent.json()["stream_id"]

        # The background task published the full stream into the replay buffer; relay
        # it over the WS and assert the lifecycle + a citation event.
        with client.websocket_connect(f"/ws/chat/{stream_id}?access_token={token}") as ws:
            events: list[dict[str, object]] = []
            for _ in range(50):
                env = ws.receive_json()
                events.append(env)
                if env["type"] in ("done", "error"):
                    break
        types = [e["type"] for e in events]
        assert types[0] == "start"
        assert types[-1] == "done"
        assert "citation" in [e.get("name") for e in events if e["type"] == "event"]

        # History reloads the assistant message WITH its citation (CC-11).
        history = client.get(f"/api/v1/chat/sessions/{sid}/messages", headers=headers)
        assert history.status_code == 200
        items = history.json()["items"]
        assistant = [m for m in items if m["role"] == "assistant"]
        assert len(assistant) == 1
        citations = assistant[0]["citations"]
        assert len(citations) == 1
        assert citations[0]["document_name"] == "taxes.pdf"
        assert citations[0]["chunk_id"] == str(seeded.alice_chunk)
        # The contract Message now carries the governed tool trace (#377): the
        # retrieval tool this turn ran is visible on the reloaded message, with
        # name + outcome only (arguments stay hashed, never exposed).
        invocations = assistant[0]["tool_invocations"]
        assert isinstance(invocations, list) and len(invocations) >= 1
        expected_keys = {"id", "tool_name", "ok", "duration_ms", "created_at"}
        assert all(set(t) >= expected_keys for t in invocations)
        assert all("args" not in t and "args_hash" not in t for t in invocations)
        # User messages carry an empty trace.
        user_items = [m for m in items if m["role"] == "user"]
        assert all(m["tool_invocations"] == [] for m in user_items)


# --- Graceful shutdown: detached, tracked answer tasks (issue #156) ----------


class _BlockingRuntime:
    """A runtime whose ``run`` never completes until cancelled.

    Models a hung/slow answer producer: it parks on a never-set ``asyncio.Event``
    so the only way the task ends is the lifespan cancelling it. ``started`` lets
    the test wait until the producer is actually executing before asserting.
    """

    def __init__(self, **_kwargs: object) -> None:
        self.started = asyncio.Event()

    async def run(self, **_kwargs: object) -> None:
        self.started.set()
        await asyncio.Event().wait()  # blocks forever unless cancelled


class _BlockingGateway:
    """A gateway whose ``stream_tools`` parks forever until cancelled.

    Drives the REAL :class:`ChatRuntime` into an in-flight state: ``run`` publishes
    ``start`` and then blocks inside the first completion turn, so a shutdown
    cancellation lands *after* ``start`` but *before* any terminal — exactly the
    SIGTERM-mid-answer window the #156 drain creates.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()

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
        self.entered.set()
        await asyncio.Event().wait()  # blocks forever unless cancelled
        yield StreamEvent(finish_reason="stop")  # pragma: no cover — unreachable


async def test_shutdown_cancellation_emits_single_retryable_terminal(
    app: FastAPI, backplane: InMemoryBackplane, seeded: _Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    # REGRESSION (#156): the lifespan drain cancels every in-flight answer producer
    # on SIGTERM. The REAL ChatRuntime.run must convert that asyncio.CancelledError
    # (a BaseException in 3.12 — it bypasses the AppError/Exception arms) into
    # exactly ONE terminal `error` envelope before re-raising, or the Redis replay
    # buffer hands a reconnecting client a start-but-no-terminal stream to wait on
    # forever. Pre-fix: the replay holds `start` and NO terminal (this asserts ==1).
    gateway = _BlockingGateway()
    monkeypatch.setattr(chat_module, "get_llm_gateway", lambda: gateway)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _login(client, seeded.alice_email)
        sid = (
            await client.post("/api/v1/chat/sessions", headers=_auth(token), json={"title": "s"})
        ).json()["id"]
        sent = await client.post(
            f"/api/v1/chat/sessions/{sid}/messages",
            headers=_auth(token),
            json={"content": "What is the 2024 standard deduction?"},
        )
        assert sent.status_code == 202, sent.text
        stream_id = sent.json()["stream_id"]

        # The producer is genuinely in-flight: `start` is published and the runtime
        # is parked inside the first completion turn.
        await asyncio.wait_for(gateway.entered.wait(), timeout=5)
        tasks: set[asyncio.Task[None]] = app.state.answer_tasks
        assert len(tasks) == 1
        answer_task = next(iter(tasks))

        # Drive the shutdown drain: cancel + await the in-flight producer.
        await _drain_answer_tasks(app, grace_seconds=5)
        assert answer_task.cancelled()

    # Drain the backplane replay for this stream: it must end with exactly one
    # terminal, and that terminal is a RETRYABLE `error` (not a `done`).
    replay = list(backplane._replay.get(stream_id, ()))
    assert replay, "producer published nothing"
    assert replay[0]["type"] == "start"
    terminals = [e for e in replay if e["type"] in ("done", "error")]
    assert len(terminals) == 1, f"expected exactly one terminal, got {[e['type'] for e in replay]}"
    terminal = terminals[0]
    assert terminal["type"] == "error"
    problem = terminal["problem"]
    assert problem["status"] == 503  # retryable; clients treat `error` as retryable
    assert problem["code"] == "dependency_unavailable"


async def test_hung_answer_does_not_block_send_or_shutdown(
    app: FastAPI, seeded: _Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    # REGRESSION (#156): a hung answer producer must NOT keep the 202 alive nor
    # block lifespan shutdown. Pre-fix the producer was a Starlette BackgroundTask
    # tied to the response and the lifespan could neither reach nor cancel it, so
    # uvicorn graceful shutdown wedged. Now the producer is a tracked asyncio.Task
    # that the lifespan cancels and drains within chat_shutdown_grace_seconds.
    blocking = _BlockingRuntime()
    monkeypatch.setattr(chat_module, "ChatRuntime", lambda **kwargs: blocking)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with lifespan(app):
            token = await _login(client, seeded.alice_email)
            sid = (
                await client.post(
                    "/api/v1/chat/sessions", headers=_auth(token), json={"title": "s"}
                )
            ).json()["id"]
            # The POST returns 202 immediately even though the producer hangs —
            # proof it is detached, not run inside the response cycle.
            sent = await asyncio.wait_for(
                client.post(
                    f"/api/v1/chat/sessions/{sid}/messages",
                    headers=_auth(token),
                    json={"content": "What is the 2024 standard deduction?"},
                ),
                timeout=5,
            )
            assert sent.status_code == 202, sent.text

            # The producer is tracked on app.state and is genuinely running.
            await asyncio.wait_for(blocking.started.wait(), timeout=5)
            tasks: set[asyncio.Task[None]] = app.state.answer_tasks
            assert len(tasks) == 1
            answer_task = next(iter(tasks))
            assert not answer_task.done()
        # Exiting the lifespan cancelled + drained the hung task within the grace
        # window rather than hanging; the set self-empties via the done callback.
        await asyncio.sleep(0)  # let the discard done-callback run
        assert answer_task.cancelled()
        assert app.state.answer_tasks == set()


def test_send_tracks_and_self_empties_answer_task(app: FastAPI, seeded: _Seeded) -> None:
    # POSITIVE (#156): a normal scripted answer is tracked on app.state.answer_tasks
    # while in flight and the set self-empties (via add_done_callback) once it
    # completes. Uses the sync TestClient (its one loop runs startup/shutdown).
    with TestClient(app) as client:
        token = client.post(
            "/api/v1/auth/login", json={"email": seeded.alice_email, "password": _PASSWORD}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        sid = client.post("/api/v1/chat/sessions", headers=headers, json={"title": "s"}).json()[
            "id"
        ]
        sent = client.post(
            f"/api/v1/chat/sessions/{sid}/messages",
            headers=headers,
            json={"content": "What is the 2024 standard deduction?"},
        )
        assert sent.status_code == 202, sent.text
        stream_id = sent.json()["stream_id"]

        # Draining the WS lets the scripted producer run to its terminal envelope;
        # once done the task discards itself from the tracking set (via the
        # add_done_callback, which the loop runs on a subsequent tick).
        with client.websocket_connect(f"/ws/chat/{stream_id}?access_token={token}") as ws:
            for _ in range(50):
                if ws.receive_json()["type"] in ("done", "error"):
                    break
        for _ in range(50):
            if not app.state.answer_tasks:
                break
            time.sleep(0.02)
        assert app.state.answer_tasks == set()


# --- Provider chat routing (PR 2a) -----------------------------------------


async def _seed_enabled_provider(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    base_url: str,
    raw_model_id: str,
    api_key: str,
) -> uuid.UUID:
    """Seed an ENABLED provider (a real vault secret for its key) + a discovered model.

    Stores the API key via the real CC-C secrets vault (the dev cipher over SQLite),
    attaches the ref to a READY llm_providers row with ``raw_model_id`` discovered.
    Returns the provider id, so the test can build the namespaced model id and assert
    the resolver decrypts the SAME key.
    """
    async with sessionmaker() as session:
        audit = AuditSink(AuditEventRepository(session, tenant_id))
        secrets = build_secrets_service(
            session,
            settings=get_settings(),
            tenant_id=tenant_id,
            owner_id=owner_id,
            roles=(Role.ADMIN,),
            audit=audit,
            request_id="seed",
            source_ip="127.0.0.1",
        )
        ref = await secrets.store_secret(
            name="llm-provider-key:seed", kind=SecretKind.OTHER, plaintext=api_key
        )
        repo = LlmProviderRepository(session, tenant_id)
        provider = await repo.create(
            owner_id=owner_id,
            name="Acme OpenAI",
            provider_type="openai_compatible",
            base_url=base_url,
            api_key_secret_ref=ref.id,
            secret_hint=ref.hint,
        )
        await repo.set_discovery(
            provider.id,
            status=LlmProviderStatus.READY,
            discovered_models=[{"id": raw_model_id, "label": "GPT-4o"}],
            last_error=None,
            last_discovery_at=None,
        )
        await session.commit()
        return provider.id


async def _drain_answer_task(app: FastAPI) -> None:
    """Await the in-flight answer producer(s) so the gateway has been called."""
    tasks: set[asyncio.Task[None]] = app.state.answer_tasks
    if tasks:
        await asyncio.gather(*list(tasks), return_exceptions=True)


async def test_send_with_provider_model_routes_through_provider(
    app: FastAPI,
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A ``provider:{id}:{raw}`` model id must reach the gateway with the RAW model id
    # + the provider's base_url as api_base + its DECRYPTED key as api_key (the real
    # resolver loads the provider + reads the CC-C vault). Never the namespaced id.
    base_url = "https://provider-a.example.com/v1"
    raw_model = "openai/gpt-4o"
    api_key = "sk-provider-secret-value-abcdef"
    pid = await _seed_enabled_provider(
        sessionmaker,
        tenant_id=seeded.tenant_a,
        owner_id=seeded.alice_id,
        base_url=base_url,
        raw_model_id=raw_model,
        api_key=api_key,
    )
    gateway = _CapturingGateway()
    monkeypatch.setattr(chat_module, "get_llm_gateway", lambda: gateway)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _login(client, seeded.alice_email)
        model_id = make_provider_model_id(pid, raw_model)
        sid = (
            await client.post(
                "/api/v1/chat/sessions",
                headers=_auth(token),
                json={"title": "s", "model": model_id},
            )
        ).json()["id"]
        sent = await client.post(
            f"/api/v1/chat/sessions/{sid}/messages", headers=_auth(token), json={"content": "hi"}
        )
        assert sent.status_code == 202, sent.text
        await _drain_answer_task(app)

    assert gateway.calls, "the gateway was never called"
    first = gateway.calls[0]
    assert first["model"] == raw_model  # RAW id, not the namespaced public id
    assert first["api_key"] == api_key  # the decrypted provider key
    assert first["api_base"] == base_url


async def test_send_with_config_model_uses_default_credentials(
    app: FastAPI,
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A plain config model id routes unchanged: the gateway gets the config id and NO
    # api_key/api_base override (None ⇒ the gateway's default OpenRouter credentials).
    gateway = _CapturingGateway()
    monkeypatch.setattr(chat_module, "get_llm_gateway", lambda: gateway)
    config_model = next(m.id for m in get_settings().chat_model_registry if m.is_default)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _login(client, seeded.alice_email)
        sid = (
            await client.post(
                "/api/v1/chat/sessions",
                headers=_auth(token),
                json={"title": "s", "model": config_model},
            )
        ).json()["id"]
        sent = await client.post(
            f"/api/v1/chat/sessions/{sid}/messages", headers=_auth(token), json={"content": "hi"}
        )
        assert sent.status_code == 202, sent.text
        await _drain_answer_task(app)

    assert gateway.calls, "the gateway was never called"
    first = gateway.calls[0]
    assert first["model"] == config_model
    assert first["api_key"] is None
    assert first["api_base"] is None


async def test_send_with_disabled_provider_model_is_422(
    app: FastAPI,
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
) -> None:
    # A session cannot even be created with a DISABLED provider's model id → 422
    # at the send/create path (INV-8), before any answer runs.
    pid = await _seed_enabled_provider(
        sessionmaker,
        tenant_id=seeded.tenant_a,
        owner_id=seeded.alice_id,
        base_url="https://provider-a.example.com/v1",
        raw_model_id="openai/gpt-4o",
        api_key="sk-x",
    )
    async with sessionmaker() as session:
        await LlmProviderRepository(session, seeded.tenant_a).update(pid, enabled=False)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _login(client, seeded.alice_email)
        resp = await client.post(
            "/api/v1/chat/sessions",
            headers=_auth(token),
            json={"model": make_provider_model_id(pid, "openai/gpt-4o")},
        )
        assert resp.status_code == 422


async def test_send_with_cross_tenant_provider_model_is_422(
    app: FastAPI,
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
) -> None:
    # Carol's (tenant B) provider id used by alice (tenant A) → unknown model → 422
    # (INV-1: alice's tenant-scoped repository never sees it).
    pid = await _seed_enabled_provider(
        sessionmaker,
        tenant_id=seeded.tenant_b,
        owner_id=seeded.carol_id,
        base_url="https://provider-b.example.com/v1",
        raw_model_id="openai/gpt-4o",
        api_key="sk-y",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _login(client, seeded.alice_email)
        resp = await client.post(
            "/api/v1/chat/sessions",
            headers=_auth(token),
            json={"model": make_provider_model_id(pid, "openai/gpt-4o")},
        )
        assert resp.status_code == 422


# Minimal valid base env so Settings constructs; the test overrides one field.
_SETTINGS_BASE = {
    "DATABASE_URL": "postgresql+asyncpg://t:t@localhost/t",
    "REDIS_URL": "redis://localhost",
    "CELERY_BROKER_URL": "redis://localhost",
    "CELERY_RESULT_BACKEND": "redis://localhost",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_ACCESS_KEY": "t",
    "S3_SECRET_KEY": "tt",
    "S3_BUCKET": "b",
}


@pytest.mark.parametrize("bad", [0, -1, -0.5])
def test_chat_shutdown_grace_must_be_positive(bad: float) -> None:
    # NEGATIVE (#156): a non-positive grace would disable the shutdown bound, so
    # Settings refuses to construct (mirrors the chat_max_tool_turns band guard).
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_SETTINGS_BASE, CHAT_SHUTDOWN_GRACE_SECONDS=bad)


def test_chat_shutdown_grace_default_is_positive() -> None:
    s = Settings(_env_file=None, **_SETTINGS_BASE)
    assert s.chat_shutdown_grace_seconds > 0
