"""Live-FK/RLS regressions; run only on the explicitly isolated PR570 database.

The caller resets lumentest_pr570 and applies migrations before every invocation.
No container operations, application database access, or shared test-role reuse.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth.principal import Principal
from app.db import models
from app.db.repositories import (
    AuditEventRepository,
    ChatSessionRepository,
    ChunkInput,
    ChunkRepository,
    CitationRepository,
    CollectionRepository,
    DocumentRepository,
    GrantRepository,
    MessageRepository,
    SessionSummaryRepository,
    TenantRepository,
    ToolInvocationRepository,
    UserRepository,
)
from app.db.tenant_context import bind_tenant
from app.domain.audit import AuditActor
from app.domain.chat import GroundedCitation
from app.domain.entities import (
    DocumentStatus,
    GrantPrincipalType,
    GrantResourceType,
    GrantRole,
    MessageRole,
    Role,
)
from app.domain.llm import ToolCall
from app.retrieval.service import RetrievalService
from app.services.audit import AuditSink
from app.services.chat_runtime import ChatRuntime
from app.services.tools.impls.recall import _read_conversation
from app.services.tools.runner import ToolRunner
from app.services.tools.types import ToolContext
from app.services.transcript_recall import SessionTranscriptReader
from tests._live_helpers import isolated_live_url, worker_database_name
from tests.test_transcript_recall import _NAME_WITHHOLD_CASES

_URL = isolated_live_url(
    "postgresql+asyncpg://lumen:lumen_local_dev@localhost:47182/lumentest_pr570"
)
_LIVE_DB = worker_database_name(_URL.rsplit("/", 1)[-1])
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE") != "1" or os.environ.get("DATABASE_URL") != _URL,
        reason="requires RUN_LIVE=1 and the isolated lumentest_pr570 DATABASE_URL",
    ),
]


@pytest_asyncio.fixture
async def factory_and_role() -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], str]]:
    # Fresh connections also prove missing-GUC denial without a pooled connection's
    # previously bound tenant value. Transaction-local roles never contaminate admin teardown.
    engine = create_async_engine(_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    role = "pr570_r2_" + uuid.uuid4().hex
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {role} NOSUPERUSER NOBYPASSRLS NOLOGIN"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"
                )
            )
        yield factory, role
    finally:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f"DROP OWNED BY {role}"))
                await conn.execute(text(f"DROP ROLE {role}"))
        finally:
            await engine.dispose()


@pytest.mark.parametrize(
    "permitted_name,revoked_name,body",
    [
        ("Project Plan", "Plan for ORION.pdf", "NAVIGATION: Project Plan for ORION.pdf"),
        ("Plan.pdf", "ORION Plan.pdf", "NAVIGATION: ORION Plan.pdf"),
    ],
)
async def test_postgres_overlapping_mentions_withhold_whole_turn(
    factory_and_role: tuple[async_sessionmaker[AsyncSession], str],
    monkeypatch: pytest.MonkeyPatch,
    permitted_name: str,
    revoked_name: str,
    body: str,
) -> None:
    from tests.test_recall_withholding import assert_overlapping_mentions_are_withheld

    factory, role = factory_and_role
    async with factory() as seed:
        tenant = await TenantRepository(seed).create(name="pr570-r9")
        users = UserRepository(seed, tenant.id)
        alice = await users.create(email="alice@r9.test", password_hash="x", roles=[Role.MEMBER])
        bob = await users.create(email="bob@r9.test", password_hash="x", roles=[Role.MEMBER])
        chat = await ChatSessionRepository(seed, tenant.id).create(owner_id=alice.id, model="m")
        collection = await CollectionRepository(seed, tenant.id).create(
            owner_id=alice.id, name="own"
        )
        document = await DocumentRepository(seed, tenant.id).create(
            owner_id=alice.id,
            collection_id=collection.id,
            filename=permitted_name,
            mime_type="text/plain",
            size_bytes=1,
            storage_key="r9-own",
            acl_enforced=False,
        )
        await seed.commit()
    principal = Principal(user_id=alice.id, tenant_id=tenant.id, roles=(Role.MEMBER,))
    async with factory() as session:
        original_commit = session.commit

        async def scoped_commit() -> None:
            await original_commit()
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            await bind_tenant(session, tenant.id)

        monkeypatch.setattr(session, "commit", scoped_commit)
        await scoped_commit()
        flags = (
            await session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        ).one()
        assert not flags.rolsuper and not flags.rolbypassrls
        await assert_overlapping_mentions_are_withheld(
            session,
            principal,
            chat.id,
            document.id,
            bob.id,
            permitted_name=permitted_name,
            revoked_name=revoked_name,
            body=body,
        )
        assert (await session.execute(text("SELECT current_user"))).scalar_one() == role


async def test_many_mentions_keep_postgres_recall_queries_and_scan_bounded(
    factory_and_role: tuple[async_sessionmaker[AsyncSession], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5-001: real PostgreSQL JSON/permissions, constant query shape, bounded work."""
    from tests.test_transcript_recall import (
        assert_many_mentions_keep_recall_queries_and_scan_bounded,
    )

    factory, role = factory_and_role
    async with factory() as seed:
        tenant = await TenantRepository(seed).create(name="pr570-r6")
        user = await UserRepository(seed, tenant.id).create(
            email="alice@r6.test", password_hash="x", roles=[Role.MEMBER]
        )
        chat = await ChatSessionRepository(seed, tenant.id).create(owner_id=user.id, model="m")
        await seed.commit()
    principal = Principal(user_id=user.id, tenant_id=tenant.id, roles=(Role.MEMBER,))
    async with factory() as session:
        original_commit = session.commit

        async def scoped_commit() -> None:
            await original_commit()
            # The shared helper commits fixture state. Re-enter the disposable
            # role and tenant on EVERY transaction, never the compose superuser.
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            await bind_tenant(session, tenant.id)

        monkeypatch.setattr(session, "commit", scoped_commit)
        await scoped_commit()
        assert (await session.execute(text("SELECT current_user"))).scalar_one() == role
        await assert_many_mentions_keep_recall_queries_and_scan_bounded(
            session, principal, chat.id, monkeypatch
        )
        assert (await session.execute(text("SELECT current_user"))).scalar_one() == role


@pytest.mark.parametrize("names,body", _NAME_WITHHOLD_CASES)
async def test_postgres_withholds_forbidden_names(
    factory_and_role: tuple[async_sessionmaker[AsyncSession], str],
    names: list[str],
    body: str,
) -> None:
    """R6-001: production JSON/permissions with a non-bypass caller and read-back."""
    from tests.test_transcript_recall import assert_recall_withholds_forbidden_names

    factory, role = factory_and_role
    async with factory() as seed:
        tenant = await TenantRepository(seed).create(name="pr570-r7")
        user = await UserRepository(seed, tenant.id).create(
            email="alice@r7.test", password_hash="x", roles=[Role.MEMBER]
        )
        chat = await ChatSessionRepository(seed, tenant.id).create(owner_id=user.id, model="m")
        await seed.commit()
    principal = Principal(user_id=user.id, tenant_id=tenant.id, roles=(Role.MEMBER,))
    async with factory() as session:
        await session.execute(text(f"SET LOCAL ROLE {role}"))
        await bind_tenant(session, tenant.id)
        assert (await session.execute(text("SELECT current_user"))).scalar_one() == role
        await assert_recall_withholds_forbidden_names(session, principal, chat.id, names, body)
        await session.commit()
    async with factory() as read_back:
        await read_back.execute(text(f"SET LOCAL ROLE {role}"))
        await bind_tenant(read_back, tenant.id)
        stored = await MessageRepository(read_back, tenant.id).list_for_session(chat.id)
        assert len(stored) == 1 and stored[0].content == body
        assert stored[0].source_document_ids == ()


@pytest.mark.parametrize("erase", ["replace", "delete"])
@pytest.mark.parametrize("source_count", [1, 2])
async def test_recalled_provenance_survives_postgres_cascades_and_revocation(
    factory_and_role: tuple[async_sessionmaker[AsyncSession], str], erase: str, source_count: int
) -> None:
    """R1-001/002: real grant checks, FK cascades, immutable DML and trace read-back."""
    factory, role = factory_and_role
    base = datetime(2021, 1, 1, tzinfo=UTC)
    async with factory() as seed:
        tenant = await TenantRepository(seed).create(name="pr570-r2")
        foreign_tenant = await TenantRepository(seed).create(name="pr570-r2-foreign")
        users = UserRepository(seed, tenant.id)
        alice = await users.create(email="alice@r2.test", password_hash="x", roles=[Role.MEMBER])
        bob = await users.create(email="bob@r2.test", password_hash="x", roles=[Role.MEMBER])
        carol = await UserRepository(seed, foreign_tenant.id).create(
            email="carol@r2.test", password_hash="x", roles=[Role.MEMBER]
        )
        chat = await ChatSessionRepository(seed, tenant.id).create(owner_id=alice.id, model="m")
        foreign_chat = await ChatSessionRepository(seed, foreign_tenant.id).create(
            owner_id=carol.id, model="m"
        )
        collection = await CollectionRepository(seed, tenant.id).create(
            owner_id=bob.id, name="plans"
        )
        docs, chunks = [], []
        for i in range(source_count):
            doc = await DocumentRepository(seed, tenant.id).create(
                owner_id=bob.id,
                collection_id=collection.id,
                filename=f"secret-plan-{i}.pdf",
                mime_type="application/pdf",
                size_bytes=3,
                storage_key=f"{tenant.id}/{i}",
                acl_enforced=False,
            )
            chunk = (
                await ChunkRepository(seed, tenant.id).replace_for_document(
                    doc.id, [ChunkInput(text="41%", char_start=0, char_end=3)]
                )
            )[0]
            await GrantRepository(seed, tenant.id).create(
                resource_type=GrantResourceType.DOCUMENT,
                resource_id=doc.id,
                principal_type=GrantPrincipalType.USER,
                principal_id=alice.id,
                role=GrantRole.VIEWER,
                granted_by=bob.id,
            )
            docs.append(doc)
            chunks.append(chunk)
        mid, clarification, unknown = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await ChatRuntime.__new__(ChatRuntime)._persist(  # noqa: SLF001
            session=seed,
            tenant_id=tenant.id,
            session_id=chat.id,
            assistant_message_id=mid,
            model="m",
            content="secret margin 41%",
            prompt_context=[],
            citations=[
                GroundedCitation(
                    document_id=d.id,
                    document_name=d.filename,
                    chunk_id=c.id,
                    snippet="41%",
                    char_start=0,
                    char_end=3,
                    score=1,
                )
                for d, c in zip(docs, chunks, strict=True)
            ],
        )
        repo = MessageRepository(seed, tenant.id)
        await repo.add_with_id(
            message_id=clarification,
            session_id=chat.id,
            role=MessageRole.ASSISTANT,
            content=f"Do you mean {docs[0].filename}?",
            source_document_ids=(),
        )
        await repo.add_with_id(
            message_id=unknown,
            session_id=chat.id,
            role=MessageRole.ASSISTANT,
            content="legacy secret with lost provenance",
        )
        for i, message_id in enumerate((mid, clarification, unknown)):
            await seed.execute(
                update(models.Message)
                .where(models.Message.id == message_id)
                .values(created_at=base + timedelta(minutes=i))
            )
        await SessionSummaryRepository(seed, tenant.id).upsert_summary(
            chat.id,
            summary=f"They discussed {docs[0].filename}",
            covers_through_message_id=unknown,
            covered_created_at=base + timedelta(minutes=2),
            mentioned_documents={docs[0].id: docs[0].filename},
        )
        await seed.commit()
    principal = Principal(user_id=alice.id, tenant_id=tenant.id, roles=(Role.MEMBER,))
    cursor = (base + timedelta(minutes=2), unknown)

    @asynccontextmanager
    async def scoped() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            await bind_tenant(session, tenant.id)
            yield session

    async def recall(query: str) -> str:
        async with scoped() as session:
            context = ToolContext(
                principal=principal,
                retrieval=RetrievalService(session, gateway=object()),
                transcript=SessionTranscriptReader(
                    session=session,
                    principal=principal,
                    session_id=chat.id,
                    compaction_cursor=cursor,
                ),
            )
            return (await _read_conversation({"query": query}, context)).content

    assert "41%" in await recall("margin")
    assert docs[0].filename in await recall("mean")
    assert "legacy secret" not in await recall("legacy")
    async with scoped() as session:
        await GrantRepository(session, tenant.id).revoke(
            resource_type=GrantResourceType.DOCUMENT,
            resource_id=docs[0].id,
            principal_type=GrantPrincipalType.USER,
            principal_id=alice.id,
        )
        await session.commit()
    assert "41%" not in await recall("margin")
    redacted = await recall("mean")
    assert docs[0].filename not in redacted
    assert "Do you mean" not in redacted
    assert "[document no longer accessible]" not in redacted
    async with scoped() as session:
        if erase == "replace":
            await ChunkRepository(session, tenant.id).replace_for_document(
                docs[0].id, [ChunkInput(text="updated", char_start=0, char_end=7)]
            )
        else:
            await session.execute(delete(models.Document).where(models.Document.id == docs[0].id))
        await session.commit()
    assert "41%" not in await recall("margin")
    async with scoped() as session:
        assert (
            len(await CitationRepository(session, tenant.id).list_for_message(mid))
            == source_count - 1
        )
        sources = await MessageRepository(session, tenant.id).source_documents_for_messages([mid])
        assert set(sources[mid]) == {d.id for d in docs}
        assert docs[0].id not in await RetrievalService(
            session, gateway=object()
        ).permitted_document_names(
            principal=principal,
            document_ids=[d.id for d in docs],
        )
        # The non-superuser cannot erase or replace a known provenance snapshot.
        with pytest.raises(DBAPIError, match="provenance is immutable"):
            async with session.begin_nested():
                await session.execute(
                    update(models.Message)
                    .where(models.Message.id == mid)
                    .values(source_document_ids=[])
                )
        # Ownership is application-enforced even though RLS is tenant-only.
        stranger = Principal(user_id=bob.id, tenant_id=tenant.id, roles=(Role.MEMBER,))
        for caller, sid in ((stranger, chat.id), (principal, foreign_chat.id)):
            denied = await SessionTranscriptReader(
                session=session, principal=caller, session_id=sid, compaction_cursor=cursor
            ).recall(query=None, limit=10, retrieval=RetrievalService(session, gateway=object()))
            assert denied.turns == ()
        flags = (
            await session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        ).one()
        assert not flags.rolsuper and not flags.rolbypassrls
        assert set(
            (await session.execute(text("SELECT tenant_id FROM chat_sessions"))).scalars()
        ) == {tenant.id}
        with pytest.raises(DBAPIError, match="row-level security"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO messages(id,tenant_id,session_id,role,content) "
                        "VALUES(:id,:tid,:sid,'user','forbidden')"
                    ),
                    {"id": uuid.uuid4(), "tid": foreign_tenant.id, "sid": foreign_chat.id},
                )
        context = ToolContext(
            principal=principal,
            retrieval=RetrievalService(session, gateway=object()),
            transcript=SessionTranscriptReader(
                session=session, principal=principal, session_id=chat.id, compaction_cursor=cursor
            ),
        )
        runner = ToolRunner(
            allowed=frozenset({"read_conversation"}),
            invocations=ToolInvocationRepository(session, tenant.id),
            audit=AuditSink(AuditEventRepository(session, tenant.id)),
            actor=AuditActor.user(alice.id),
            request_id="pr570-r2",
            source_ip="unknown",
            session_id=chat.id,
        )
        results = [
            await runner.run(
                call=ToolCall(id=f"r2-{i}", name="read_conversation", arguments={"query": q}),
                context=context,
                message_id=mid,
            )
            for i, q in enumerate(("margin", "margin", "mean", "third"))
        ]
        assert [r.ok for r in results] == [True, False, True, False]
        await session.commit()
    async with scoped() as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM tool_invocations WHERE session_id=:sid"),
                {"sid": chat.id},
            )
        ).scalar_one() == 4
        assert (
            await session.execute(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE action IN ('tool.invoked','tool.result')"
                )
            )
        ).scalar_one() == 8
        await session.execute(delete(models.Message).where(models.Message.id == mid))
        await session.commit()
    assert "secret margin" not in await recall("margin")
    async with factory() as session:
        await session.execute(text(f"SET LOCAL ROLE {role}"))
        assert not (await session.execute(text("SELECT id FROM messages"))).all()


@pytest.mark.parametrize("ending", ["answer", "question"])
@pytest.mark.parametrize("source", ["citation", "mention"])
async def test_recall_only_answer_is_withheld_after_postgres_grant_revocation(
    factory_and_role: tuple[async_sessionmaker[AsyncSession], str], ending: str, source: str
) -> None:
    """R2-001: actual tool/runtime, grant revocation and immutable source read-back."""
    from tests.test_chat_runtime import _run_recall_only_answer

    factory, role = factory_and_role
    async with factory() as seed:
        tenant = await TenantRepository(seed).create(name="pr570-r3")
        users = UserRepository(seed, tenant.id)
        alice = await users.create(email="alice@r3.test", password_hash="x", roles=[Role.MEMBER])
        bob = await users.create(email="bob@r3.test", password_hash="x", roles=[Role.MEMBER])
        chat = await ChatSessionRepository(seed, tenant.id).create(owner_id=alice.id, model="m")
        coll = await CollectionRepository(seed, tenant.id).create(owner_id=bob.id, name="plans")
        doc = await DocumentRepository(seed, tenant.id).create(
            owner_id=bob.id,
            collection_id=coll.id,
            filename="secret-q3-plan.pdf",
            mime_type="application/pdf",
            size_bytes=3,
            storage_key=f"{tenant.id}/plan",
            acl_enforced=False,
        )
        chunk = (
            await ChunkRepository(seed, tenant.id).replace_for_document(
                doc.id, [ChunkInput(text="41%", char_start=0, char_end=3)]
            )
        )[0]
        await GrantRepository(seed, tenant.id).create(
            resource_type=GrantResourceType.DOCUMENT,
            resource_id=doc.id,
            principal_type=GrantPrincipalType.USER,
            principal_id=alice.id,
            role=GrantRole.VIEWER,
            granted_by=bob.id,
        )
        await seed.commit()
    principal = Principal(user_id=alice.id, tenant_id=tenant.id, roles=(Role.MEMBER,))

    @asynccontextmanager
    async def scoped() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            await bind_tenant(session, tenant.id)
            yield session

    original, copied, needle = await _run_recall_only_answer(
        sessionmaker=scoped,
        principal=principal,
        session_id=chat.id,
        document_id=doc.id,
        chunk_id=chunk.id,
        retrieval_factory=lambda session: RetrievalService(session, gateway=object()),
        ending=ending,
        source=source,
    )

    async def recall() -> str:
        async with scoped() as session:
            message = await MessageRepository(session, tenant.id).get(copied)
            assert message is not None
            context = ToolContext(
                principal=principal,
                retrieval=RetrievalService(session, gateway=object()),
                transcript=SessionTranscriptReader(
                    session=session,
                    principal=principal,
                    session_id=chat.id,
                    compaction_cursor=(message.created_at, copied),
                ),
            )
            return (await _read_conversation({}, context)).content

    assert needle in await recall(), "granted recall of the copied answer is the positive control"
    async with scoped() as session:
        flags = (
            await session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        ).one()
        assert not flags.rolsuper and not flags.rolbypassrls
        await session.execute(delete(models.Grant).where(models.Grant.resource_id == doc.id))
        await session.commit()
    assert needle not in await recall(), "both original and copied prose must obey current grants"
    async with scoped() as session:
        assert (
            await RetrievalService(session, gateway=object()).permitted_document_names(
                principal=principal, document_ids=[doc.id]
            )
            == {}
        )
        sources = await MessageRepository(session, tenant.id).source_documents_for_messages(
            [original, copied]
        )
        assert sources[copied] == (doc.id,)
        assert await CitationRepository(session, tenant.id).list_for_message(copied) == []
        with pytest.raises(DBAPIError, match="provenance is immutable"):
            async with session.begin_nested():
                await session.execute(
                    update(models.Message)
                    .where(models.Message.id == copied)
                    .values(source_document_ids=[])
                )


@pytest.mark.parametrize("path", ["read-answer", "read-question", "history", "recall-history"])
async def test_complete_prompt_provenance_obeys_postgres_revocation(
    factory_and_role: tuple[async_sessionmaker[AsyncSession], str], path: str
) -> None:
    """R3-001/002: actual reads/grants and transitive history under the RLS role."""
    from tests.test_chat_runtime import _run_recall_only_answer
    from tests.test_prompt_provenance import compact_and_recall, live_history, run_context_answer

    factory, role = factory_and_role
    async with factory() as seed:
        tenant = await TenantRepository(seed).create(name="pr570-r4")
        users = UserRepository(seed, tenant.id)
        alice = await users.create(email="alice@r4.test", password_hash="x", roles=[Role.MEMBER])
        bob = await users.create(email="bob@r4.test", password_hash="x", roles=[Role.MEMBER])
        chat = await ChatSessionRepository(seed, tenant.id).create(owner_id=alice.id, model="m")
        coll = await CollectionRepository(seed, tenant.id).create(owner_id=bob.id, name="plans")
        doc = await DocumentRepository(seed, tenant.id).create(
            owner_id=bob.id,
            collection_id=coll.id,
            filename="secret-q3-plan.pdf",
            mime_type="application/pdf",
            size_bytes=26,
            storage_key=f"{tenant.id}/plan",
            acl_enforced=False,
            status=DocumentStatus.READY,
        )
        chunk = (
            await ChunkRepository(seed, tenant.id).replace_for_document(
                doc.id, [ChunkInput(text="The secret margin was 41%.", char_start=0, char_end=26)]
            )
        )[0]
        await GrantRepository(seed, tenant.id).create(
            resource_type=GrantResourceType.DOCUMENT,
            resource_id=doc.id,
            principal_type=GrantPrincipalType.USER,
            principal_id=alice.id,
            role=GrantRole.VIEWER,
            granted_by=bob.id,
        )
        await seed.commit()
    principal = Principal(user_id=alice.id, tenant_id=tenant.id, roles=(Role.MEMBER,))

    @asynccontextmanager
    async def scoped() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            await bind_tenant(session, tenant.id)
            yield session

    ids = []
    if path == "recall-history":
        original, recalled, _ = await _run_recall_only_answer(
            sessionmaker=scoped,
            principal=principal,
            session_id=chat.id,
            document_id=doc.id,
            chunk_id=chunk.id,
            retrieval_factory=lambda session: RetrievalService(session, gateway=object()),
            ending="answer",
            source="citation",
            compact_copy=False,
        )
        ids.extend([original, recalled])
    else:
        ids.append(
            await run_context_answer(
                factory=scoped,
                principal=principal,
                session_id=chat.id,
                retrieval_factory=lambda session: RetrievalService(session, gateway=object()),
                call=ToolCall(
                    id="document", name="get_document", arguments={"document_id": str(doc.id)}
                ),
                ending="question" if path == "read-question" else "answer",
            )
        )
    if path in {"history", "recall-history"}:
        for _ in range(2 if path == "history" else 1):
            ids.append(
                await run_context_answer(
                    factory=scoped,
                    principal=principal,
                    session_id=chat.id,
                    retrieval_factory=lambda session: RetrievalService(session, gateway=object()),
                    history=await live_history(scoped, principal, chat.id),
                )
            )

    async with scoped() as session:
        retrieval = RetrievalService(session, gateway=object())
        granted = await compact_and_recall(scoped, principal, chat.id, ids[-1], retrieval)
        assert "41%" in granted.content
        assert granted.passages == () and granted.document_ids == ()
        flags = (
            await session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        ).one()
        assert not flags.rolsuper and not flags.rolbypassrls
        await session.execute(delete(models.Grant).where(models.Grant.resource_id == doc.id))
        await session.commit()
    async with scoped() as session:
        retrieval = RetrievalService(session, gateway=object())
        assert await retrieval.get_document(principal=principal, document_id=doc.id) is None
        assert (
            await retrieval.permitted_document_names(principal=principal, document_ids=[doc.id])
            == {}
        )
        revoked = await compact_and_recall(scoped, principal, chat.id, ids[-1], retrieval)
        assert "41%" not in revoked.content, "every derived generation must obey revocation"
        assert "withheld_turns" not in revoked.payload
        sources = await MessageRepository(session, tenant.id).source_documents_for_messages(ids)
        assert all(sources[mid] == (doc.id,) for mid in ids)
