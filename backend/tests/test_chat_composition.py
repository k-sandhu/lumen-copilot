"""One chat composition + a real ``write_file`` artifact writer (#576).

Regression coverage for the three divergences the bug shipped:

* ``ChatRuntime`` was built in three places and none wired an artifact
  writer, so a real chat's ``write_file`` always reported "no artifact
  writer". These tests drive the shared composition end-to-end (scripted
  gateway, in-memory SQLite, fake object store) and assert exactly one
  durable artifact row + object, with round-tripped bytes/checksum.
* The tenant tool policy was decided three different ways (Admin listing,
  the runtime's offered set, the approval gate). One
  ``effective_tool_decision`` now feeds all three: a tenant-disabled tool
  is never offered, and a T1 tool with no override — which Admin shows as
  enabled with approval off — is not denied ``tool_policy_absent`` under
  ``act_with_approval``. T2 semantics are unchanged.
* ``ArtifactsService.create_artifact`` stored the object before the row,
  orphaning the object when registration failed. Creation is now
  compensating: a failed registration leaves no object (and never deletes
  an object an earlier artifact already references).
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.core.config import get_settings
from app.db import models
from app.db.repositories import (
    ArtifactRepository,
    AuditEventRepository,
    ChatSessionRepository,
    TenantRepository,
    TenantSandboxPolicyRepository,
    TenantToolPolicyRepository,
    UserRepository,
)
from app.domain.entities import ArtifactProducedBy, AutonomyLevel, Role, TenantToolPolicy
from app.domain.llm import StreamEvent, ToolCall
from app.domain.tools import RiskTier
from app.realtime.backplane import InMemoryBackplane
from app.services.artifacts_service import ArtifactsService
from app.services.assistant_runtime import AssistantRunConfig
from app.services.audit import AuditSink
from app.services.chat_composition import (
    ChatCompositionOptions,
    build_artifact_writer_factory,
    build_chat_runtime,
)
from app.services.chat_runtime import ArtifactWriterContext, SandboxContext
from app.services.tool_policy_service import ToolPolicyService, effective_tool_decision
from app.services.tools.gate import PolicyApprovalGate
from app.services.tools.impls.write_file import _write_file
from app.services.tools.types import ApprovalRequest, ToolContext
from app.storage.keys import assert_artifact_key_owned_by, build_artifact_key
from tests._audit_helpers import RecordingDurableAuditTransactions, denial_context
from tests._db_helpers import copy_sqlite_schema

import app.db.models  # noqa: F401  isort: skip

_ALLOWED = frozenset({"text/plain", "text/markdown", "text/csv", "application/json"})
_CANARY = "QA576-ARTIFACT canary body"


class _FakeStore:
    """No-network stand-in for the object store's artifact methods."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    async def put_artifact(
        self,
        tenant_id: str,
        data: bytes,
        content_type: str,
        filename: str,
        *,
        object_id: str | None = None,
    ) -> object:
        from app.storage.object_store import StoredObject

        key = build_artifact_key(tenant_id, data, filename, object_id=object_id)
        self.objects[(tenant_id, key)] = data
        return StoredObject(
            key=key,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            content_type=content_type,
        )

    async def get_artifact(self, tenant_id: str, key: str) -> bytes:
        assert_artifact_key_owned_by(key, tenant_id)
        return self.objects[(tenant_id, key)]

    async def delete_artifact(self, tenant_id: str, key: str) -> None:
        self.objects.pop((tenant_id, key), None)


class _FakeRetrieval:
    """Minimal retrieval fake — ``write_file`` never searches."""

    async def search_text(self, **_kwargs: object) -> list[object]:
        return []

    async def permitted_document_names(self, **_kwargs: object) -> dict[uuid.UUID, str]:
        return {}

    async def valid_chunk_pairs(self, **_kwargs: object) -> dict[uuid.UUID, uuid.UUID]:
        return {}


class _ScriptedGateway:
    """Replays scripted turns; records the tool specs each turn was offered."""

    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self._turns = turns
        self._index = 0
        self.offered: list[list[str]] = []

    async def stream_tools(
        self, messages: object, *, tools: object, **_kwargs: object
    ) -> AsyncIterator[StreamEvent]:
        specs = tools if isinstance(tools, list | tuple) else []
        self.offered.append([str(getattr(s, "name", s)) for s in specs])
        turn = self._turns[min(self._index, len(self._turns) - 1)]
        self._index += 1
        for stream_event in turn:
            yield stream_event

    async def chat(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("no scripted chat completion")


class _World:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        alice_id: uuid.UUID,
        bob_id: uuid.UUID,
        session_id: uuid.UUID,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.tenant_id = tenant_id
        self.alice_id = alice_id
        self.bob_id = bob_id
        self.session_id = session_id
        self.store = _FakeStore()

    @property
    def alice(self) -> Principal:
        return Principal(user_id=self.alice_id, tenant_id=self.tenant_id, roles=(Role.MEMBER,))

    def artifacts_service(
        self, session: AsyncSession, *, owner_id: uuid.UUID | None = None
    ) -> ArtifactsService:
        owner = owner_id or self.alice_id
        return ArtifactsService(
            session,
            tenant_id=self.tenant_id,
            owner_id=owner,
            object_store=self.store,  # type: ignore[arg-type]
            audit=AuditSink(AuditEventRepository(session, self.tenant_id)),
            denials=denial_context(
                RecordingDurableAuditTransactions(),
                session,
                self.tenant_id,
                owner,
                request_id="req-576",
                source_ip="127.0.0.1",
            ),
            request_id="req-576",
            source_ip="127.0.0.1",
            artifact_allowed_content_types=_ALLOWED,
            max_artifact_bytes=100_000,
        )


@pytest_asyncio.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_World]:
    # These regressions exercise composition/governance/storage, not a provider
    # tokenizer or its model catalog. Keep context sizing deterministic/offline.
    monkeypatch.setattr(
        "app.services.chat_runtime.litellm_token_counter",
        lambda _model: lambda text: max(1, len(text) // 4),
    )
    monkeypatch.setattr("app.llm.context.litellm_max_input_tokens", lambda _model: None)
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    # SQLite's legacy transaction mode otherwise commits an outermost SAVEPOINT
    # on RELEASE, even though the answer transaction has not committed. Mirror
    # the production transactional semantics for rollback regression coverage.
    @event.listens_for(engine.sync_engine, "connect")
    def explicit_transactions(connection: object, _record: object) -> None:
        connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def begin(connection: object) -> None:
        connection.exec_driver_sql("BEGIN")

    try:
        async with engine.begin() as conn:
            await conn.run_sync(copy_sqlite_schema)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as seed:
            tenant = await TenantRepository(seed).create(name="Acme")
            alice = await UserRepository(seed, tenant.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            bob = await UserRepository(seed, tenant.id).create(
                email="bob@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            chat = await ChatSessionRepository(seed, tenant.id).create(
                owner_id=alice.id, model="test-model", title="t"
            )
            await seed.commit()
            yield _World(
                sessionmaker=factory,
                tenant_id=tenant.id,
                alice_id=alice.id,
                bob_id=bob.id,
                session_id=chat.id,
            )
    finally:
        await engine.dispose()
        get_settings.cache_clear()


async def _count(world: _World, model: type) -> int:
    async with world.sessionmaker() as session:
        stmt = select(func.count()).select_from(model)
        return int((await session.execute(stmt)).scalar_one())


async def _audit_actions(world: _World) -> list[str]:
    async with world.sessionmaker() as session:
        events = await AuditEventRepository(session, world.tenant_id).list_recent(limit=100)
        return [e.action for e in events]


def _write_file_config(autonomy: AutonomyLevel = AutonomyLevel.ACT_AUTO) -> AssistantRunConfig:
    return AssistantRunConfig(
        system_prompt="You are a test assistant.",
        allowed=frozenset({"write_file"}),
        collection_ids=None,
        model=None,
        autonomy=autonomy,
    )


def _write_then_answer_gateway() -> _ScriptedGateway:
    return _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(
                        ToolCall(
                            id="c1",
                            name="write_file",
                            arguments={
                                "filename": "result.txt",
                                "content_type": "text/plain",
                                "content": _CANARY,
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [StreamEvent(text="Done — the file is written."), StreamEvent(finish_reason="stop")],
        ]
    )


async def _run_composed(
    world: _World,
    *,
    mode: str,
    gateway: _ScriptedGateway,
    config: AssistantRunConfig,
    simulate_writes: bool = False,
) -> InMemoryBackplane:
    settings = get_settings()
    backplane = InMemoryBackplane()
    runtime = build_chat_runtime(
        mode=mode,  # type: ignore[arg-type]
        principal=world.alice,
        backplane=backplane,  # type: ignore[arg-type]
        settings=settings,
        sessionmaker=world.sessionmaker,
        gateway=gateway,  # type: ignore[arg-type]
        request_id="req-576",
        source_ip="127.0.0.1",
        options=ChatCompositionOptions(
            object_store_factory=lambda: world.store,  # type: ignore[return-value]
            extra_runtime_kwargs={"retrieval_factory": lambda _s: _FakeRetrieval()},
        ),
    )
    await runtime.run(
        stream_id=uuid.uuid4().hex,
        session_id=world.session_id,
        question="Write the result file.",
        model="test-model",
        history=[],
        collection_ids=None,
        assistant_config=config,
        simulate_writes=simulate_writes,
    )
    return backplane


# ---------------------------------------------------------------------------
# One effective decision (Admin listing / runtime offer / approval gate).
# ---------------------------------------------------------------------------


def test_effective_decision_no_override_matches_admin_defaults() -> None:
    t1 = effective_tool_decision(
        tool_name="write_file", override=None, registry_requires_approval=False
    )
    assert t1.enabled is True
    assert t1.requires_approval is False
    assert t1.is_default is True

    t2 = effective_tool_decision(
        tool_name="run_python", override=None, registry_requires_approval=True
    )
    assert t2.enabled is True
    assert t2.requires_approval is True
    assert t2.is_default is True


async def test_admin_listing_uses_effective_decision(world: _World) -> None:
    async with world.sessionmaker() as session:
        entries = await ToolPolicyService(session, tenant_id=world.tenant_id).list_policy()
    by_name = {e.tool_name: e for e in entries}
    assert by_name["write_file"].enabled is True
    assert by_name["write_file"].requires_approval is False


@pytest.mark.parametrize("tool_name", ["write_file", "mcp:demo:note"])
async def test_gate_allows_t1_with_no_override(world: _World, tool_name: str) -> None:
    """Regression: T1 under act_with_approval was denied ``tool_policy_absent``."""
    async with world.sessionmaker() as session:
        gate = PolicyApprovalGate(session, tenant_id=world.tenant_id)
        decision = await gate.request(
            ApprovalRequest(
                call_id="c1",
                tool_name=tool_name,
                risk_tier=RiskTier.T1,
                principal=world.alice,
                arguments={"filename": "a.txt"},
                arguments_hash="h",
            )
        )
    assert decision.approved is True


@pytest.mark.parametrize("tool_name", ["run_python", "mcp:demo:write"])
async def test_gate_still_denies_t2_with_no_override(world: _World, tool_name: str) -> None:
    async with world.sessionmaker() as session:
        gate = PolicyApprovalGate(session, tenant_id=world.tenant_id)
        decision = await gate.request(
            ApprovalRequest(
                call_id="c1",
                tool_name=tool_name,
                risk_tier=RiskTier.T2,
                principal=world.alice,
                arguments={"code": "pass"},
                arguments_hash="h",
            )
        )
    assert decision.approved is False
    assert decision.reason == "tool_policy_absent"


async def test_gate_denies_tenant_disabled_tool(world: _World) -> None:
    async with world.sessionmaker() as session:
        await TenantToolPolicyRepository(session, world.tenant_id).upsert(
            tool_name="write_file",
            enabled=False,
            requires_approval=False,
            updated_by=world.alice_id,
        )
        await session.commit()
    async with world.sessionmaker() as session:
        gate = PolicyApprovalGate(session, tenant_id=world.tenant_id)
        decision = await gate.request(
            ApprovalRequest(
                call_id="c1",
                tool_name="write_file",
                risk_tier=RiskTier.T1,
                principal=world.alice,
                arguments={},
                arguments_hash="h",
            )
        )
    assert decision.approved is False
    assert decision.reason == "tool_policy_disabled"


# ---------------------------------------------------------------------------
# Compensating artifact creation (no orphan object or row).
# ---------------------------------------------------------------------------


async def test_failed_duplicate_registration_keeps_existing_object(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world.sessionmaker() as session:
        svc = world.artifacts_service(session)
        first = await svc.create_artifact(
            data=_CANARY.encode(),
            filename="result.txt",
            content_type="text/plain",
            produced_by=ArtifactProducedBy.TOOL,
        )
        await session.commit()
        assert len(world.store.objects) == 1

        async def _boom(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("row insert failed")

        monkeypatch.setattr(ArtifactRepository, "create", _boom)
        await _run_composed(
            world,
            mode="interactive",
            gateway=_write_then_answer_gateway(),
            config=_write_file_config(),
        )
        content = await svc.get_artifact_content(first.id)
    # The earlier artifact's content-addressed object must survive compensation.
    assert content is not None
    assert content.data == _CANARY.encode()
    assert len(world.store.objects) == 1
    assert await _count(world, models.Artifact) == 1


# ---------------------------------------------------------------------------
# The bound writer factory (immutable origin) + handler round trip.
# ---------------------------------------------------------------------------


async def test_bound_writer_round_trips_and_stamps_origin(world: _World) -> None:
    settings = get_settings()
    run_id = uuid.uuid4()
    factory = build_artifact_writer_factory(
        principal=world.alice,
        settings=settings,
        request_id="req-576",
        source_ip="127.0.0.1",
        sessionmaker=world.sessionmaker,
        run_id=run_id,
        object_store_factory=lambda: world.store,  # type: ignore[return-value]
    )
    async with world.sessionmaker() as session:
        writer = factory(
            ArtifactWriterContext(
                session=session,
                session_id=world.session_id,
                message_id=uuid.uuid4(),
            )
        )
        assert writer is not None
        ctx = ToolContext(
            principal=world.alice,
            retrieval=_FakeRetrieval(),  # type: ignore[arg-type]
            artifacts=writer,
            session_id=world.session_id,
        )
        result = await _write_file(
            {"filename": "result.txt", "content_type": "text/plain", "content": _CANARY},
            ctx,
        )
        await session.commit()
        assert result.ok is True
        assert result.payload["size_bytes"] == len(_CANARY.encode())
        assert result.payload["sha256"] == hashlib.sha256(_CANARY.encode()).hexdigest()
        artifact_id = uuid.UUID(result.payload["artifact_id"])

        svc = world.artifacts_service(session)
        row = await svc.get_artifact(artifact_id)
        assert row is not None
        assert row.session_id == world.session_id
        assert row.run_id == run_id
        assert row.owner_id == world.alice_id
        content = await svc.get_artifact_content(artifact_id)
        assert content is not None
        assert content.data == _CANARY.encode()

        # INV-2: another user in the same tenant gets the missing shape (→404).
        other = world.artifacts_service(session, owner_id=world.bob_id)
        assert await other.get_artifact(artifact_id) is None
        assert await other.get_artifact_content(artifact_id) is None


# ---------------------------------------------------------------------------
# Composition: interactive / headless write for real; test simulates.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["interactive", "headless"])
async def test_composed_runtime_creates_exactly_one_artifact(world: _World, mode: str) -> None:
    gateway = _write_then_answer_gateway()
    await _run_composed(world, mode=mode, gateway=gateway, config=_write_file_config())

    assert await _count(world, models.Artifact) == 1
    assert len(world.store.objects) == 1
    actions = await _audit_actions(world)
    assert actions.count("artifact.created") == 1
    assert actions.count("tool.invoked") == 1
    assert actions.count("tool.result") == 1
    async with world.sessionmaker() as session:
        artifact = (await session.execute(select(models.Artifact))).scalar_one()
        invocation = (await session.execute(select(models.ToolInvocation))).scalar_one()
        assert artifact.tool_invocation_id == invocation.id
        message = await session.get(models.Message, invocation.message_id)
        assert message is not None and message.session_id == world.session_id
        events = await AuditEventRepository(session, world.tenant_id).list_recent(limit=100)
        created = next(item for item in events if item.action == "artifact.created")
        assert created.actor_id == world.alice_id
        assert created.metadata["session_id"] == str(world.session_id)
        assert created.metadata["message_id"] == str(message.id)
        assert created.metadata["tool_invocation_id"] == str(invocation.id)
    # write_file was offered to the model on the first turn.
    assert "write_file" in gateway.offered[0]


async def test_composed_test_mode_creates_nothing_durable(world: _World) -> None:
    gateway = _write_then_answer_gateway()
    await _run_composed(
        world,
        mode="test",
        gateway=gateway,
        config=_write_file_config(),
        simulate_writes=True,
    )
    assert await _count(world, models.Artifact) == 0
    assert world.store.objects == {}


async def test_tenant_disabled_write_file_is_never_offered(world: _World) -> None:
    async with world.sessionmaker() as session:
        await TenantToolPolicyRepository(session, world.tenant_id).upsert(
            tool_name="write_file",
            enabled=False,
            requires_approval=False,
            updated_by=world.alice_id,
        )
        await session.commit()
    gateway = _write_then_answer_gateway()
    await _run_composed(world, mode="interactive", gateway=gateway, config=_write_file_config())
    assert all("write_file" not in offered for offered in gateway.offered)
    assert await _count(world, models.Artifact) == 0


async def test_act_with_approval_t1_no_override_executes(world: _World) -> None:
    gateway = _write_then_answer_gateway()
    await _run_composed(
        world,
        mode="interactive",
        gateway=gateway,
        config=_write_file_config(AutonomyLevel.ACT_WITH_APPROVAL),
    )
    assert await _count(world, models.Artifact) == 1


async def test_headless_composition_wires_sandbox_factory(world: _World) -> None:
    """Headless runs gain the sandbox seam under the owner identity (#576/#578)."""
    captured: dict[str, object] = {}

    def _factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    build_chat_runtime(
        mode="headless",
        principal=world.alice,
        backplane=InMemoryBackplane(),  # type: ignore[arg-type]
        settings=get_settings(),
        sessionmaker=world.sessionmaker,
        gateway=object(),  # type: ignore[arg-type]
        request_id="req-576",
        source_ip="127.0.0.1",
        options=ChatCompositionOptions(runtime_factory=_factory),
    )
    assert captured["sandbox_factory"] is not None
    assert captured["artifact_writer_factory"] is not None
    assert captured["mcp_tools_factory"] is not None
    assert captured["interactive"] is False

    captured.clear()
    build_chat_runtime(
        mode="test",
        principal=world.alice,
        backplane=InMemoryBackplane(),  # type: ignore[arg-type]
        settings=get_settings(),
        sessionmaker=world.sessionmaker,
        gateway=object(),  # type: ignore[arg-type]
        request_id="req-576",
        source_ip="127.0.0.1",
        options=ChatCompositionOptions(runtime_factory=_factory),
    )
    # Assistant Test keeps its by-design absences: no sandbox, no real writer,
    # no MCP — and the serial session discipline of the preview harness.
    assert captured["sandbox_factory"] is None
    assert captured["artifact_writer_factory"] is None
    assert captured["mcp_tools_factory"] is None
    assert captured["tool_concurrency"] == 1


@pytest.mark.parametrize(
    "failure", ["before_commit", "before_upload", "after_upload", "registration", "audit"]
)
async def test_failed_write_or_answer_commit_leaves_no_orphan(
    world: _World, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "before_commit":
        commit = AsyncSession.commit

        async def fail_commit(session: AsyncSession) -> None:
            if world.store.objects:
                raise RuntimeError("commit unavailable: private connection details")
            await commit(session)

        monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    elif failure in ("before_upload", "after_upload"):
        put = world.store.put_artifact

        async def fail_upload(*args: object, **kwargs: object) -> object:
            if failure == "after_upload":
                await put(*args, **kwargs)
            raise RuntimeError("upload acknowledgement lost: private storage details")

        monkeypatch.setattr(world.store, "put_artifact", fail_upload)
    elif failure == "registration":

        async def fail_create(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("insert failed: private query details")

        monkeypatch.setattr(ArtifactRepository, "create", fail_create)
    else:
        emit = AuditSink.emit

        async def fail_audit(sink: AuditSink, **kwargs: object) -> object:
            result = await emit(sink, **kwargs)
            if kwargs.get("action") == "artifact.created":
                raise RuntimeError("audit failed after flush: private details")
            return result

        monkeypatch.setattr(AuditSink, "emit", fail_audit)
    backplane = await _run_composed(
        world, mode="interactive", gateway=_write_then_answer_gateway(), config=_write_file_config()
    )
    assert world.store.objects == {}
    assert await _count(world, models.Artifact) == 0
    assert "artifact.created" not in await _audit_actions(world)
    assert "private" not in str(backplane._replay)


async def test_assistant_denied_write_is_never_offered_or_executed(world: _World) -> None:
    from dataclasses import replace

    gateway = _write_then_answer_gateway()
    await _run_composed(
        world,
        mode="interactive",
        gateway=gateway,
        config=replace(_write_file_config(), allowed=frozenset()),
    )
    assert all("write_file" not in offered for offered in gateway.offered)
    assert world.store.objects == {}
    assert await _count(world, models.Artifact) == 0


async def test_t1_explicit_approval_policy_applies_under_act_auto(world: _World) -> None:
    async with world.sessionmaker() as session:
        await TenantToolPolicyRepository(session, world.tenant_id).upsert(
            tool_name="write_file",
            enabled=True,
            requires_approval=True,
            updated_by=world.alice_id,
        )
        await session.commit()
    await _run_composed(
        world, mode="interactive", gateway=_write_then_answer_gateway(), config=_write_file_config()
    )
    assert await _count(world, models.Artifact) == 0
    assert world.store.objects == {}


@pytest.mark.parametrize("mode", ["interactive", "headless"])
@pytest.mark.parametrize("change_turn", [0, 1])
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("disable", "tool_policy_disabled"),
        ("require_approval", "approval_required_unavailable"),
        ("unreadable", "tool_policy_unreadable"),
    ],
)
async def test_t1_dispatch_rechecks_policy_after_tool_offer(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    change_turn: int,
    change: str,
    reason: str,
) -> None:
    """R1-001: offering a tool cannot authorise a later revoked write."""
    changed = False
    reads: list[bool] = []

    async def current_policy(
        repository: TenantToolPolicyRepository, tool_name: str
    ) -> TenantToolPolicy | None:
        assert repository._tenant_id == world.tenant_id
        assert tool_name == "write_file"
        reads.append(changed)
        if not changed:
            return None
        if change == "unreadable":
            raise RuntimeError("private policy connection details")
        now = datetime.now(UTC)
        return TenantToolPolicy(
            id=uuid.uuid4(),
            tenant_id=world.tenant_id,
            tool_name=tool_name,
            enabled=change != "disable",
            requires_approval=change == "require_approval",
            updated_by=world.alice_id,
            created_at=now,
            updated_at=now,
        )

    monkeypatch.setattr(TenantToolPolicyRepository, "get_by_tool", current_policy)
    script = _write_then_answer_gateway()

    class RevokingGateway(_ScriptedGateway):
        async def stream_tools(
            self, messages: object, *, tools: object, **kwargs: object
        ) -> AsyncIterator[StreamEvent]:
            nonlocal changed
            # Deterministic provider/dispatch handshake: the runtime has already
            # offered write_file, but has not received this turn's tool call.
            if self._index == change_turn:
                assert "write_file" in [getattr(spec, "name", None) for spec in tools]
                changed = True
            async for item in super().stream_tools(messages, tools=tools, **kwargs):
                yield item

    gateway = RevokingGateway([script._turns[0]] * (change_turn + 1) + script._turns[1:])
    backplane = await _run_composed(world, mode=mode, gateway=gateway, config=_write_file_config())
    # A prior successful write survives; the revoked dispatch creates nothing.
    assert await _count(world, models.Artifact) == change_turn
    assert len(world.store.objects) == change_turn
    assert reads == [False] * change_turn + [True]
    actions = await _audit_actions(world)
    assert actions.count("artifact.created") == change_turn
    assert actions.count("tool.invoked") == change_turn + 1
    assert actions.count("tool.result") == change_turn + 1
    async with world.sessionmaker() as session:
        invocations = list(
            (
                await session.execute(
                    select(models.ToolInvocation).order_by(models.ToolInvocation.ordinal)
                )
            )
            .scalars()
            .all()
        )
        assert invocations[-1].error == "approval_denied"
        assert reason in invocations[-1].result_summary
    assert "private" not in str(backplane._replay)


@pytest.mark.parametrize("enabled", [True, False])
async def test_headless_sandbox_runs_as_owner_under_tenant_policy(
    world: _World, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    from functools import partial

    from app.domain.entities import CodeRunStatus
    from app.sandbox.accounting import InMemorySandboxAccounting
    from app.sandbox.tool_runner import ChatSandboxToolRunner
    from tests._sandbox_helpers import sandbox_settings
    from tests.test_sandbox_tool_runner import _FakeRunner, _ok_result

    runner = _FakeRunner(result=_ok_result())
    monkeypatch.setattr("app.services.chat_composition.HttpSandboxRunner", lambda *a, **kw: runner)
    # Accounting is an injected adapter alongside the fake runner in this offline test.
    monkeypatch.setattr(
        "app.services.chat_composition.ChatSandboxToolRunner",
        partial(ChatSandboxToolRunner, accounting=InMemorySandboxAccounting()),
    )
    async with world.sessionmaker() as session:
        await TenantSandboxPolicyRepository(session, world.tenant_id).upsert(
            enabled=enabled,
            allowed_packages=(),
            denied_packages=(),
            egress_allowed=False,
            egress_allowlist=(),
            max_runtime_s=30,
            max_memory_mb=512,
            daily_runtime_cap_s=3600,
            max_concurrency=2,
            updated_by=world.alice_id,
        )
        await session.commit()
    runtime = build_chat_runtime(
        mode="headless",
        principal=world.alice,
        backplane=InMemoryBackplane(),
        settings=sandbox_settings(),
        sessionmaker=world.sessionmaker,
        gateway=object(),
        request_id="req-576",
        source_ip="127.0.0.1",
        options=ChatCompositionOptions(object_store_factory=lambda: world.store),
    )
    assert runtime._sandbox_factory is not None
    seam = runtime._sandbox_factory(
        SandboxContext(
            stream_id="headless",
            session_id=world.session_id,
            message_id=uuid.uuid4(),
            next_seq=lambda: 0,
        )
    )
    assert seam is not None
    result = await seam.submit(code="print('hello')")
    assert result.status == (CodeRunStatus.SUCCEEDED if enabled else CodeRunStatus.DENIED)
    assert runner.calls == (1 if enabled else 0)
    async with world.sessionmaker() as session:
        run = (await session.execute(select(models.CodeRun))).scalar_one()
        assert run.owner_id == world.alice_id
        assert run.tenant_id == world.tenant_id
        assert run.session_id == world.session_id


async def test_lost_commit_ack_preserves_committed_artifact(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = AsyncSession.commit

    async def lost_ack(session: AsyncSession) -> None:
        await commit(session)
        if world.store.objects:
            raise RuntimeError("commit acknowledged too late")

    monkeypatch.setattr(AsyncSession, "commit", lost_ack)
    await _run_composed(
        world, mode="interactive", gateway=_write_then_answer_gateway(), config=_write_file_config()
    )
    assert await _count(world, models.Artifact) == 1
    assert len(world.store.objects) == 1


async def test_cancelled_answer_rolls_back_and_compensates_uploaded_file(world: _World) -> None:
    after_write = asyncio.Event()

    class PausedGateway(_ScriptedGateway):
        async def stream_tools(self, *args: object, **kwargs: object) -> AsyncIterator[StreamEvent]:
            if self._index == 1:
                after_write.set()
                await asyncio.Future()
            async for stream_event in super().stream_tools(*args, **kwargs):
                yield stream_event

    gateway = PausedGateway(_write_then_answer_gateway()._turns)
    task = asyncio.create_task(
        _run_composed(world, mode="interactive", gateway=gateway, config=_write_file_config())
    )
    await after_write.wait()
    assert len(world.store.objects) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert world.store.objects == {}
    assert await _count(world, models.Artifact) == 0
    assert "artifact.created" not in await _audit_actions(world)


async def test_generated_artifact_api_round_trip_and_foreign_ids_return_404(world: _World) -> None:
    from app.api.deps import current_user, get_db_session, get_object_store_dep, get_settings_dep
    from app.main import create_app

    await _run_composed(
        world, mode="interactive", gateway=_write_then_answer_gateway(), config=_write_file_config()
    )
    async with world.sessionmaker() as session:
        artifact = (await session.execute(select(models.Artifact))).scalar_one()
        artifact_id = artifact.id

    async def session_dependency() -> AsyncIterator[AsyncSession]:
        async with world.sessionmaker() as session:
            yield session
            await session.commit()

    app = create_app()
    inline_settings = get_settings().model_copy(update={"document_content_redirect": False})
    app.dependency_overrides[get_settings_dep] = lambda: inline_settings
    app.dependency_overrides[get_db_session] = session_dependency
    app.dependency_overrides[get_object_store_dep] = lambda: world.store
    app.dependency_overrides[current_user] = lambda: world.alice
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/v1/artifacts/{artifact_id}"
        response = await client.get(path + "/content")
        assert response.status_code == 200
        assert response.content == _CANARY.encode()
        response = await client.get(path)
        assert response.status_code == 200
        assert response.json()["size_bytes"] == len(_CANARY.encode())
        listing = await client.get(f"/api/v1/artifacts?session_id={world.session_id}")
        assert [item["id"] for item in listing.json()["items"]] == [str(artifact_id)]
        for principal in (
            Principal(user_id=world.bob_id, tenant_id=world.tenant_id, roles=(Role.MEMBER,)),
            Principal(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), roles=(Role.MEMBER,)),
        ):
            app.dependency_overrides[current_user] = lambda principal=principal: principal
            for suffix in ("", "/content"):
                foreign = await client.get(path + suffix)
                missing = await client.get(f"/api/v1/artifacts/{uuid.uuid4()}" + suffix)
                assert foreign.status_code == missing.status_code == 404
                assert foreign.json()["code"] == missing.json()["code"]
        app.dependency_overrides[current_user] = lambda: world.alice
        assert (await client.delete(path)).status_code == 204
        assert (await client.get(path)).status_code == 404
    assert world.store.objects == {}
    assert await _count(world, models.Artifact) == 0
