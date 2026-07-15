"""Conversational agent builder API tests — POST /assistants/draft (issue #213).

End-to-end against the real FastAPI app over an offline in-memory SQLite DB (no
Postgres / Redis / real model), mirroring the assistants API tests. The LLM
gateway is overridden with a scripted fake so the draft flow is deterministic and
offline. Covers the additive ``contracts/openapi.yaml`` ``/assistants/draft``
surface:

* AC-1: a plain-language description yields a valid draft config that pre-fills the
  editor (name/instructions/model/scope/tools/autonomy);
* AC-2: a description missing scope/owner surfaces clarifying questions rather than
  a silent guess;
* AC-N (negative): a tool the model invents that is NOT registered, and a
  collection/source id the caller cannot see, are **omitted** from the draft with
  an explanatory note (deny-by-default, INV-2) — never surfaced;
* AC-3: a drafted high-risk (write-tier) tool surfaces a warning;
* a blank/oversize description → 422 (INV-8); no token → 401 (INV-4);
* the builder degrades to a safe minimal draft when the gateway is disabled.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_backplane_dep, get_db_session, get_llm_gateway_dep
from app.auth import hash_password
from app.db.base import Base
from app.db.repositories import (
    CollectionRepository,
    SourceRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import Role, SourceStatus
from app.domain.llm import ChatMessage, Completion, Embedding, TokenUsage
from app.main import create_app
from app.realtime.backplane import InMemoryBackplane

import app.db.models  # noqa: F401  isort: skip

_PASSWORD = "devpassword"


class _Seeded:
    def __init__(
        self,
        *,
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        alice: uuid.UUID,
        carol: uuid.UUID,
        alice_collection: uuid.UUID,
        alice_source: uuid.UUID,
        carol_collection: uuid.UUID,
    ) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice = alice
        self.carol = carol
        self.alice_collection = alice_collection
        self.alice_source = alice_source
        self.carol_collection = carol_collection
        self.alice_email = "alice@acme.test"


class _ScriptedGateway:
    """A fake LLM gateway that returns a fixed JSON completion (offline, AC-1).

    The service asks for a bare JSON object; this returns whatever ``payload`` was
    scripted so a test can drive the exact draft shape. ``enabled`` is True so the
    service takes the model path (a disabled gateway is a separate test).
    """

    enabled = True

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    async def chat(
        self, messages: Sequence[ChatMessage], *, model: str | None = None
    ) -> Completion:
        return Completion(content=json.dumps(self._payload), model="fake", usage=TokenUsage())

    async def embed(
        self,
        inputs: list[str],
        *,
        cache_namespace: str | None = None,
    ) -> list[Embedding]:  # pragma: no cover
        return [Embedding(vector=[0.0], model="fake") for _ in inputs]


class _DisabledGateway:
    """A gateway with no provider key — the fail-soft degrade path."""

    enabled = False

    async def chat(
        self, messages: Sequence[ChatMessage], *, model: str | None = None
    ) -> Completion:  # pragma: no cover — never called when disabled
        raise AssertionError("disabled gateway must not be called")

    async def embed(
        self,
        inputs: list[str],
        *,
        cache_namespace: str | None = None,
    ) -> list[Embedding]:  # pragma: no cover
        return [Embedding(vector=[0.0], model="fake") for _ in inputs]


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
                email="alice@acme.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            carol = await UserRepository(seed, tb.id).create(
                email="carol@globex.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            coll_a = await CollectionRepository(seed, ta.id).create(
                owner_id=alice.id, name="HR handbook"
            )
            src_a = await SourceRepository(seed, ta.id).create(
                owner_id=alice.id,
                type="notion",
                config={"workspace": "acme"},
                status=SourceStatus.READY,
            )
            # Carol's collection lives in tenant B — the cross-tenant fixture: the
            # builder must never surface it in Alice's draft (INV-1/INV-2).
            coll_c = await CollectionRepository(seed, tb.id).create(
                owner_id=carol.id, name="Globex secrets"
            )
            await seed.commit()
            factory.lumen_seeded = _Seeded(  # type: ignore[attr-defined]
                tenant_a=ta.id,
                tenant_b=tb.id,
                alice=alice.id,
                carol=carol.id,
                alice_collection=coll_a.id,
                alice_source=src_a.id,
                carol_collection=coll_c.id,
            )
            yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> _Seeded:
    return sessionmaker.lumen_seeded  # type: ignore[attr-defined, no-any-return]


def _make_app(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    gateway: object,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    application = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_db_session] = _override_session
    application.dependency_overrides[get_backplane_dep] = lambda: InMemoryBackplane()
    application.dependency_overrides[get_llm_gateway_dep] = lambda: gateway

    import app.main as main_module

    class _NoopStore:
        async def ensure_bucket(self) -> None:
            return None

    monkeypatch.setattr(main_module, "get_object_store", lambda: _NoopStore())
    return application


@pytest_asyncio.fixture
async def client_for(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    """A factory that builds a client whose app uses the given scripted gateway."""

    async def _build(gateway: object) -> tuple[AsyncClient, FastAPI]:
        application = _make_app(sessionmaker, gateway=gateway, monkeypatch=monkeypatch)
        transport = ASGITransport(app=application)
        return AsyncClient(transport=transport, base_url="http://test"), application

    return _build


async def _login(client: AsyncClient, email: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- AC-1: a description yields a valid draft that pre-fills the editor ------


async def test_draft_returns_valid_config(client_for, seeded: _Seeded) -> None:
    payload = {
        "name": "Benefits helper",
        "description": "Answers HR benefits questions",
        "instructions": "You are a friendly benefits assistant. Cite the policy.",
        "model": "openrouter/some-model",
        "collectionIds": [str(seeded.alice_collection)],
        "sourceIds": [str(seeded.alice_source)],
        "toolAllowlist": ["search_documents", "get_document"],
        "autonomyLevel": "draft",
    }
    client, _ = await client_for(_ScriptedGateway(payload))
    async with client:
        token = await _login(client, seeded.alice_email)
        resp = await client.post(
            "/api/v1/assistants/draft",
            headers=_auth(token),
            json={"description": "An assistant that answers HR benefits questions."},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        draft = body["draft"]
        # AC-1: the draft is a full editor-loadable config in contract shape.
        assert draft["name"] == "Benefits helper"
        assert draft["instructions"].startswith("You are a friendly benefits assistant")
        assert draft["model"] == "openrouter/some-model"
        assert draft["autonomyLevel"] == "draft"
        assert draft["toolAllowlist"] == ["search_documents", "get_document"]
        # The caller-owned collection + source survive validation.
        assert draft["knowledgeScope"]["collectionIds"] == [str(seeded.alice_collection)]
        assert draft["knowledgeScope"]["sourceIds"] == [str(seeded.alice_source)]
        # Owner is always asked (a clarifying question, never guessed — AC-2).
        assert any("owner" in q.lower() for q in body["clarifications"])


# --- AC-2: missing scope surfaces a clarifying question, not a guess --------


async def test_missing_scope_surfaces_clarification(client_for, seeded: _Seeded) -> None:
    # The model returns a config with NO scope — the builder must ask, not invent.
    payload = {"name": "Vague bot", "instructions": "Be helpful."}
    client, _ = await client_for(_ScriptedGateway(payload))
    async with client:
        token = await _login(client, seeded.alice_email)
        resp = await client.post(
            "/api/v1/assistants/draft",
            headers=_auth(token),
            json={"description": "Make me a helpful assistant."},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["draft"]["knowledgeScope"]["collectionIds"] == []
        assert body["draft"]["knowledgeScope"]["sourceIds"] == []
        # AC-2: a missing scope triggers a clarifying question (not a silent guess).
        assert any(
            "collection" in q.lower() or "source" in q.lower() for q in body["clarifications"]
        )
        assert any("owner" in q.lower() for q in body["clarifications"])


# --- AC-N (negative): unknown tool + unseeable scope are OMITTED with a note -


async def test_unknown_tool_and_foreign_scope_are_omitted(client_for, seeded: _Seeded) -> None:
    payload = {
        "name": "Over-broad bot",
        "instructions": "Do everything.",
        # A tool that is NOT registered + a real registered one.
        "toolAllowlist": ["definitely_not_a_tool", "search_documents"],
        # A collection Alice does NOT own (Carol's, tenant B) + a bogus id.
        "collectionIds": [str(seeded.carol_collection), "not-a-uuid"],
        # A source id that does not exist for Alice.
        "sourceIds": [str(uuid.uuid4())],
    }
    client, _ = await client_for(_ScriptedGateway(payload))
    async with client:
        token = await _login(client, seeded.alice_email)
        resp = await client.post(
            "/api/v1/assistants/draft",
            headers=_auth(token),
            json={"description": "An assistant that can do anything to anything."},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        draft = body["draft"]
        # The unknown tool is dropped; the real one survives (deny-by-default).
        assert draft["toolAllowlist"] == ["search_documents"]
        # The foreign / bogus scope ids are dropped — never surfaced (INV-2).
        assert draft["knowledgeScope"]["collectionIds"] == []
        assert draft["knowledgeScope"]["sourceIds"] == []
        assert str(seeded.carol_collection) not in json.dumps(draft)
        # Transparency notes explain each omission.
        joined = " ".join(body["notes"]).lower()
        assert "tool" in joined
        assert "collection" in joined
        assert "source" in joined


# --- AC-3: a drafted high-risk tool surfaces a warning ----------------------


async def test_high_risk_tool_surfaces_warning(client_for, seeded: _Seeded) -> None:
    # write_file is a T1 (non-read-only) write-tier tool — high risk.
    payload = {"name": "Writer", "toolAllowlist": ["write_file"]}
    client, _ = await client_for(_ScriptedGateway(payload))
    async with client:
        token = await _login(client, seeded.alice_email)
        resp = await client.post(
            "/api/v1/assistants/draft",
            headers=_auth(token),
            json={"description": "An assistant that writes files for me."},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["draft"]["toolAllowlist"] == ["write_file"]
        # AC-3: the high-risk tool is warned about before publish.
        assert any("write_file" in w for w in body["warnings"])
        # And a risk-acknowledgement clarifying question is asked (E6-3).
        assert any(
            "risk" in q.lower() or "acknowledge" in q.lower() for q in body["clarifications"]
        )


# --- Fail-soft: a disabled gateway degrades to a safe minimal draft ---------


async def test_disabled_gateway_degrades_to_minimal_draft(client_for, seeded: _Seeded) -> None:
    client, _ = await client_for(_DisabledGateway())
    async with client:
        token = await _login(client, seeded.alice_email)
        resp = await client.post(
            "/api/v1/assistants/draft",
            headers=_auth(token),
            json={"description": "A renewals reminder assistant."},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The description becomes the instructions; scope + tools stay empty (safe).
        assert body["draft"]["instructions"] == "A renewals reminder assistant."
        assert body["draft"]["toolAllowlist"] == []
        assert body["draft"]["autonomyLevel"] == "suggest"
        # It still asks the user to fill the gaps.
        assert len(body["clarifications"]) >= 1


# --- Guardrails: 422 on blank/oversize body; 401 without a token ------------


async def test_blank_description_is_422(client_for, seeded: _Seeded) -> None:
    client, _ = await client_for(_ScriptedGateway({"name": "x"}))
    async with client:
        token = await _login(client, seeded.alice_email)
        resp = await client.post(
            "/api/v1/assistants/draft", headers=_auth(token), json={"description": ""}
        )
        assert resp.status_code == 422, resp.text


async def test_draft_requires_auth(client_for, seeded: _Seeded) -> None:
    client, _ = await client_for(_ScriptedGateway({"name": "x"}))
    async with client:
        resp = await client.post("/api/v1/assistants/draft", json={"description": "hi"})
        assert resp.status_code == 401, resp.text
