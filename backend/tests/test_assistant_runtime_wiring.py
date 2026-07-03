"""Assistant → run wiring — the assembler + the no-widen guarantee (issue #211).

Two layers:

* **Unit** — :mod:`app.services.assistant_runtime` is pure assembly over a frozen
  version config: it derives the instructions-augmented system prompt, the
  registry-intersected allowed-tool set, and the narrowing collection scope. Plus
  the runtime's ``_narrow_collection_ids`` intersection helper (INV-2: scope may
  only narrow, never widen).

* **Integration** — the :class:`~app.services.chat_runtime.ChatRuntime` end-to-end
  offline with a scripted gateway + a fake retrieval that **records the
  collection_ids it was called with**. This proves AC-2 (the run uses the
  assistant's instructions/tools/scope) and AC-3 (an assistant scoped to
  collection A cannot retrieve from B even when the model asks — scope narrows
  only, INV-2), and AC-4 (an off-list tool is unreachable at runtime).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.db.base import Base
from app.db.repositories import (
    ChatSessionRepository,
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import Role
from app.domain.llm import StreamEvent, ToolCall
from app.domain.retrieval import DocumentMatch, DocumentText, RetrievedPassage
from app.realtime.backplane import InMemoryBackplane
from app.services.assistant_runtime import (
    assemble_run_config,
    build_system_prompt,
    resolve_allowlist,
    scope_collection_ids,
)
from app.services.chat_runtime import ChatRuntime, _narrow_collection_ids
from app.services.prompts import GROUNDED_SYSTEM_PROMPT
from app.services.tools.registry import default_allowlist

import app.db.models  # noqa: F401  isort: skip


# ---------------------------------------------------------------------------
# Unit: the assembler.
# ---------------------------------------------------------------------------


def test_build_system_prompt_prepends_instructions() -> None:
    out = build_system_prompt("You are a tax expert.")
    assert out.startswith("You are a tax expert.")
    # The grounding contract (INV-3) is preserved after the persona.
    assert GROUNDED_SYSTEM_PROMPT in out


def test_build_system_prompt_blank_is_bare_grounded() -> None:
    assert build_system_prompt(None) == GROUNDED_SYSTEM_PROMPT
    assert build_system_prompt("   ") == GROUNDED_SYSTEM_PROMPT


def test_resolve_allowlist_empty_is_default() -> None:
    assert resolve_allowlist([]) == default_allowlist()


def test_resolve_allowlist_intersects_registry() -> None:
    # A known tool survives; an unknown one is dropped (deny by default).
    resolved = resolve_allowlist(["search_text", "not_a_tool"])
    assert "search_text" in resolved
    assert "not_a_tool" not in resolved


def test_resolve_allowlist_keeps_mcp_names() -> None:
    # A namespaced MCP tool (#227) is NOT in the static registry (it is tenant-
    # scoped + dynamic), so it must be kept as-is here — whether it is actually
    # offered/invokable is decided per-run by the runtime resolver + the runner.
    resolved = resolve_allowlist(["mcp:srv-abc123def456:echo", "not_a_tool"])
    assert "mcp:srv-abc123def456:echo" in resolved
    assert "not_a_tool" not in resolved


def test_scope_collection_ids_parses_and_empty_is_none() -> None:
    cid = uuid.uuid4()
    assert scope_collection_ids({"collectionIds": [str(cid)]}) == [cid]
    assert scope_collection_ids({"collectionIds": []}) is None
    assert scope_collection_ids({}) is None


def test_assemble_run_config_from_frozen_config() -> None:
    cid = uuid.uuid4()
    config = {
        "name": "Tax helper",
        "instructions": "Be precise.",
        "model": "anthropic/claude-opus-4.8",
        "knowledgeScope": {"collectionIds": [str(cid)], "sourceIds": [], "modes": []},
        "toolAllowlist": ["search_text"],
        "autonomyLevel": "suggest",
    }
    cfg = assemble_run_config(config)
    assert cfg.model == "anthropic/claude-opus-4.8"
    assert cfg.collection_ids == [cid]
    assert cfg.allowed == frozenset({"search_text"})
    assert cfg.system_prompt.startswith("Be precise.")


# ---------------------------------------------------------------------------
# Unit: the no-widen intersection (INV-2 — scope narrows only).
# ---------------------------------------------------------------------------


def test_narrow_none_both_is_none() -> None:
    assert _narrow_collection_ids(None, None) is None


def test_narrow_only_assistant_scope() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    assert _narrow_collection_ids(None, [a, b]) == [a, b]


def test_narrow_only_send_scope() -> None:
    a = uuid.uuid4()
    assert _narrow_collection_ids([a], None) == [a]


def test_narrow_both_intersects() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # Send asks for {a, b}; assistant scope allows {b, c} — only b survives.
    assert _narrow_collection_ids([a, b], [b, c]) == [b]


def test_narrow_disjoint_is_empty_not_widened() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    # No overlap ⇒ empty list (retrieves nothing), NEVER a fallback to unfiltered.
    assert _narrow_collection_ids([a], [b]) == []


# ---------------------------------------------------------------------------
# Integration: ChatRuntime honours the assistant config (AC-2/AC-3/AC-4).
# ---------------------------------------------------------------------------


class _RecordingRetrieval:
    """A retrieval stand-in that records the collection_ids each search saw."""

    def __init__(self, passages: list[RetrievedPassage]) -> None:
        self._passages = passages
        self.seen_collection_ids: list[list[uuid.UUID] | None] = []

    async def search_text(
        self, *, principal: object, query: str, k: int, collection_ids: object = None
    ) -> list[RetrievedPassage]:
        self.seen_collection_ids.append(
            list(collection_ids) if collection_ids is not None else None  # type: ignore[arg-type]
        )
        return list(self._passages)

    async def search_documents(
        self, *, principal: object, name_or_query: str, k: int = 10
    ) -> list[DocumentMatch]:
        return []

    async def get_document(self, *, principal: object, document_id: object) -> DocumentText | None:
        return None


class _OneSearchThenAnswer:
    """A gateway that calls the requested tool once, then answers tool-free."""

    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name

    async def stream_tools(
        self, messages: object, *, tools: object, model: object = None, tool_choice: object = None
    ) -> AsyncIterator[StreamEvent]:
        msgs = list(messages)  # type: ignore[arg-type]
        has_tool_result = any(getattr(m, "role", None).value == "tool" for m in msgs)
        if not has_tool_result and tool_choice != "none":
            yield StreamEvent(
                tool_calls=(ToolCall(id="c1", name=self._tool_name, arguments={"query": "q"}),),
                finish_reason="tool_calls",
            )
        else:
            yield StreamEvent(text="Answer.")
            yield StreamEvent(finish_reason="stop")


class _Ctx:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        principal: Principal,
        session_id: uuid.UUID,
        collection_a: uuid.UUID,
        collection_b: uuid.UUID,
        document_id: uuid.UUID,
        chunk_id: uuid.UUID,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.principal = principal
        self.session_id = session_id
        self.collection_a = collection_a
        self.collection_b = collection_b
        self.document_id = document_id
        self.chunk_id = chunk_id


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
            coll_a = await CollectionRepository(seed, tenant.id).create(
                owner_id=user.id, name="A"
            )
            coll_b = await CollectionRepository(seed, tenant.id).create(
                owner_id=user.id, name="B"
            )
            doc = await DocumentRepository(seed, tenant.id).create(
                owner_id=user.id,
                collection_id=coll_a.id,
                filename="a.pdf",
                mime_type="application/pdf",
                size_bytes=10,
                storage_key=f"{tenant.id}/a.pdf",
            )
            chunks = await ChunkRepository(seed, tenant.id).replace_for_document(
                doc.id, [ChunkInput(text="A content.", char_start=0, char_end=10)]
            )
            session = await ChatSessionRepository(seed, tenant.id).create(
                owner_id=user.id, model="anthropic/claude-opus-4.8", title="t"
            )
            await seed.commit()
            yield _Ctx(
                sessionmaker=factory,
                principal=Principal(
                    user_id=user.id, tenant_id=tenant.id, roles=(Role.MEMBER,)
                ),
                session_id=session.id,
                collection_a=coll_a.id,
                collection_b=coll_b.id,
                document_id=doc.id,
                chunk_id=chunks[0].id,
            )
    finally:
        await engine.dispose()


def _runtime(ctx: _Ctx, *, gateway: object, retrieval: object) -> ChatRuntime:
    return ChatRuntime(
        sessionmaker=ctx.sessionmaker,
        gateway=gateway,  # type: ignore[arg-type]
        backplane=InMemoryBackplane(),
        principal=ctx.principal,
        request_id="req-1",
        source_ip="127.0.0.1",
        default_max_tool_turns=4,
        retrieval_factory=lambda _session: retrieval,  # type: ignore[arg-type,return-value]
    )


async def test_assistant_scope_narrows_retrieval_to_collection_a(ctx: _Ctx) -> None:
    """AC-3 / INV-2: an assistant scoped to A retrieves only from A, even when the send names B."""
    passage = RetrievedPassage(
        chunk_id=ctx.chunk_id,
        document_id=ctx.document_id,
        document_name="a.pdf",
        ord=0,
        text="A content.",
        char_start=0,
        char_end=10,
        score=0.9,
    )
    retrieval = _RecordingRetrieval([passage])
    gateway = _OneSearchThenAnswer("search_text")
    runtime = _runtime(ctx, gateway=gateway, retrieval=retrieval)

    config = assemble_run_config(
        {
            "name": "A-only",
            "instructions": "Scoped.",
            "model": None,
            "knowledgeScope": {"collectionIds": [str(ctx.collection_a)]},
            "toolAllowlist": ["search_text"],
            "autonomyLevel": "suggest",
        }
    )

    await runtime.run(
        stream_id="s1",
        session_id=ctx.session_id,
        question="tell me",
        model="anthropic/claude-opus-4.8",
        history=[],
        # The send names collection B — the assistant scope (A) must narrow it to
        # nothing (disjoint), NEVER widen to include B.
        collection_ids=[ctx.collection_b],
        assistant_config=config,
    )

    # Retrieval was called with the *narrowed* scope: the intersection of {B} (send)
    # and {A} (assistant) is empty — B was never reachable.
    assert retrieval.seen_collection_ids == [[]]


async def test_assistant_scope_alone_scopes_to_collection_a(ctx: _Ctx) -> None:
    """AC-2: with no send scope, the assistant's scope (A) is the effective filter."""
    passage = RetrievedPassage(
        chunk_id=ctx.chunk_id,
        document_id=ctx.document_id,
        document_name="a.pdf",
        ord=0,
        text="A content.",
        char_start=0,
        char_end=10,
        score=0.9,
    )
    retrieval = _RecordingRetrieval([passage])
    gateway = _OneSearchThenAnswer("search_text")
    runtime = _runtime(ctx, gateway=gateway, retrieval=retrieval)

    config = assemble_run_config(
        {
            "name": "A-only",
            "instructions": "Scoped.",
            "model": None,
            "knowledgeScope": {"collectionIds": [str(ctx.collection_a)]},
            "toolAllowlist": ["search_text"],
            "autonomyLevel": "suggest",
        }
    )
    await runtime.run(
        stream_id="s2",
        session_id=ctx.session_id,
        question="tell me",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
        assistant_config=config,
    )
    assert retrieval.seen_collection_ids == [[ctx.collection_a]]


async def test_off_list_tool_is_unreachable_at_runtime(ctx: _Ctx) -> None:
    """AC-4: a tool not in the assistant's allow-list returns a not-permitted result.

    The assistant allows only ``search_documents``; the model asks for
    ``search_text`` — the governed runner refuses it (``tool_not_permitted``) and
    ``search_text`` is never invoked (the recording retrieval sees no text search).
    """
    retrieval = _RecordingRetrieval([])
    gateway = _OneSearchThenAnswer("search_text")
    runtime = _runtime(ctx, gateway=gateway, retrieval=retrieval)

    config = assemble_run_config(
        {
            "name": "Docs only",
            "instructions": "x",
            "model": None,
            "knowledgeScope": {},
            # search_text is NOT in this allow-list.
            "toolAllowlist": ["search_documents"],
            "autonomyLevel": "suggest",
        }
    )
    assert "search_text" not in config.allowed
    await runtime.run(
        stream_id="s3",
        session_id=ctx.session_id,
        question="tell me",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
        assistant_config=config,
    )
    # search_text was refused by the runner before the handler ran, so the
    # recording retrieval's search_text was never called.
    assert retrieval.seen_collection_ids == []
