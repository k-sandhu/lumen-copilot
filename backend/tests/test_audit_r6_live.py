"""R6 regressions on migrated PostgreSQL, only in the reserved lumentest_pr604 DB."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.auth.principal import Principal
from app.db import models
from app.db.audit_transactions import DurableAuditTransactions
from app.db.repositories import (
    AuditEventRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    UserRepository,
)
from app.db.tenant_context import bind_bypass, bind_tenant
from app.domain.audit import AuditActor
from app.domain.entities import AuditOutcome, DocumentStatus, Role
from app.services.audit import AuditSink, PermissionDeniedContext, PermissionDeniedRecorder
from app.services.collections_service import CollectionsService
from tests._live_helpers import isolated_live_url, worker_database_name

_LIVE_URL = os.environ.get("AUDIT_DENIAL_LIVE_DATABASE_URL")
if _LIVE_URL is not None:
    _LIVE_URL = isolated_live_url(_LIVE_URL, "AUDIT_DENIAL_LIVE_DATABASE_URL")
_LIVE_DB = worker_database_name((_LIVE_URL or "lumentest_pr604").rsplit("/", 1)[-1])
pytestmark = pytest.mark.skipif(_LIVE_URL is None, reason="Targeted PostgreSQL opt-in required")


class _Store:
    async def delete(self, tenant_id: str, key: str) -> None:
        pass


@dataclass
class _Live:
    factory: async_sessionmaker[AsyncSession]
    transactions: DurableAuditTransactions
    principal: Principal
    foreign: Principal

    def service(self, caller: AsyncSession, request_id: str) -> CollectionsService:
        return CollectionsService(
            caller,
            tenant_id=self.principal.tenant_id,
            owner_id=self.principal.user_id,
            object_store=_Store(),  # type: ignore[arg-type]
            audit=AuditSink(AuditEventRepository(caller, self.principal.tenant_id)),
            denials=PermissionDeniedContext(
                PermissionDeniedRecorder(
                    self.transactions, tenant_id=self.principal.tenant_id, request_session=caller
                ),
                principal=self.principal,
                request_id=request_id,
                source_ip="203.0.113.79",
            ),
        )


@pytest_asyncio.fixture
async def live(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Live]:
    assert _LIVE_URL is not None
    parsed = urlparse(_LIVE_URL)
    assert parsed.path == f"/{_LIVE_DB}"
    assert os.environ["DATABASE_URL"] == _LIVE_URL
    admin_url = urlunparse(parsed._replace(path="/postgres"))
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    role = f"lumen_pr604_r6_{uuid.uuid4().hex[:8]}"
    owner_engine = None
    request_engine = None
    provider = None
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("app.tasks.enqueue_index_sync", lambda *args: None)
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {_LIVE_DB} WITH (FORCE)"))
            await conn.execute(text(f"CREATE DATABASE {_LIVE_DB}"))
            await conn.execute(
                text(f"CREATE ROLE {role} LOGIN PASSWORD 'r6_disposable' NOSUPERUSER NOBYPASSRLS")
            )
        root = Path(__file__).resolve().parent.parent
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "alembic"))
        await asyncio.to_thread(command.upgrade, config, "head")
        owner_engine = create_async_engine(_LIVE_URL)
        async with owner_engine.begin() as conn:
            await conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"
                )
            )
            await conn.execute(text(f"REVOKE UPDATE, DELETE ON audit_events FROM {role}"))
        owner_factory = async_sessionmaker(owner_engine, expire_on_commit=False)
        async with owner_factory() as seed:
            await bind_bypass(seed)
            tenant = await TenantRepository(seed).create(name="R6 tenant")
            other = await TenantRepository(seed).create(name="R6 foreign")
            actor = await UserRepository(seed, tenant.id).create(
                email="r6@audit.invalid", password_hash="not-a-password", roles=[Role.MEMBER]
            )
            foreign = await UserRepository(seed, other.id).create(
                email="r6foreign@audit.invalid", password_hash="not-a-password", roles=[Role.MEMBER]
            )
            await seed.commit()
        app_url = urlunparse(
            parsed._replace(netloc=f"{role}:r6_disposable@{parsed.hostname}:{parsed.port}")
        )
        request_engine = create_async_engine(app_url, pool_size=4, max_overflow=0)
        audit_engine = create_async_engine(app_url, pool_size=2, max_overflow=0)
        provider = DurableAuditTransactions(audit_engine, operation_timeout_seconds=15)
        factory = async_sessionmaker(request_engine, expire_on_commit=False, autoflush=False)
        async with factory() as check:
            flags = (
                await check.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
                )
            ).one()
            assert tuple(flags) == (False, False)
            await bind_tenant(check, other.id)
            assert (
                await check.execute(select(models.User).where(models.User.id == actor.id))
            ).scalar_one_or_none() is None
        yield _Live(
            factory,
            provider,
            Principal(actor.id, tenant.id, (Role.MEMBER,)),
            Principal(foreign.id, other.id, (Role.MEMBER,)),
        )
    finally:
        if provider is not None:
            await provider.dispose()
        if request_engine is not None:
            await request_engine.dispose()
        if owner_engine is not None:
            await owner_engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {_LIVE_DB} WITH (FORCE)"))
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin.dispose()
        get_settings.cache_clear()


def _pause_deletion(monkeypatch, caller, entered, resume):
    # Direct-upload cleanup acquires the parent lock at the first get (#606).
    # Pause before that lock: a later competing delete or FK insert must block.
    name = "get"
    original = getattr(CollectionRepository, name)

    async def paused(repo, *args, **kwargs):
        if repo._session is caller:
            entered.set()
            await resume.wait()
        return await original(repo, *args, **kwargs)

    monkeypatch.setattr(CollectionRepository, name, paused)


async def _collection(live: _Live):
    async with live.factory() as seed:
        await bind_tenant(seed, live.principal.tenant_id)
        collection = await CollectionRepository(seed, live.principal.tenant_id).create(
            owner_id=live.principal.user_id, name="R6 collection"
        )
        await seed.commit()
    return collection


async def test_late_collection_absence_has_durable_denial_after_caller_rollback(live, monkeypatch):
    """R5-001: A commits deletion while B is paused at the real repository entry."""
    collection = await _collection(live)
    entered, resume = asyncio.Event(), asyncio.Event()
    async with live.factory() as loser:
        await bind_tenant(loser, live.principal.tenant_id)
        pending = await CollectionRepository(loser, live.principal.tenant_id).create(
            owner_id=live.principal.user_id, name="must roll back"
        )
        _pause_deletion(monkeypatch, loser, entered, resume)
        task = asyncio.create_task(live.service(loser, "r6-loser").delete(collection.id))
        try:
            await asyncio.wait_for(entered.wait(), 30)
            async with live.factory() as winner:
                await bind_tenant(winner, live.principal.tenant_id)
                assert await live.service(winner, "r6-winner").delete(collection.id)
                await winner.commit()
            resume.set()
            assert await asyncio.wait_for(task, 30) is False
            await loser.rollback()
        finally:
            resume.set()
            await asyncio.gather(task, return_exceptions=True)
    async with live.factory() as read:
        await bind_tenant(read, live.principal.tenant_id)
        assert await CollectionRepository(read, live.principal.tenant_id).get(pending.id) is None
        rows = await AuditEventRepository(read, live.principal.tenant_id).list_recent()
        assert sorted((row.request_id, row.action) for row in rows) == [
            ("r6-loser", "permission.denied"),
            ("r6-winner", "collection.deleted"),
        ]
        denial = next(row for row in rows if row.request_id == "r6-loser")
        assert denial.actor_id == live.principal.user_id
        assert denial.resource_id == str(collection.id)
        assert denial.source_origin == "client"
        assert str(denial.source_ip) == "203.0.113.79"
        assert denial.metadata == {"attempted_action": "collection.delete", "reason": "not_visible"}


@pytest.mark.parametrize("commit", [True, False])
async def test_concurrent_upload_is_in_exact_cascade_audit(live, monkeypatch, commit):
    """R5-001: uploader commits between the old snapshot and repository delete."""
    collection = await _collection(live)
    entered, resume = asyncio.Event(), asyncio.Event()
    async with live.factory() as deleter:
        await bind_tenant(deleter, live.principal.tenant_id)
        cached = (
            await deleter.execute(
                select(models.Collection)
                .where(models.Collection.id == collection.id)
                .options(selectinload(models.Collection.documents))
            )
        ).scalar_one()
        assert cached.documents == []
        _pause_deletion(monkeypatch, deleter, entered, resume)
        task = asyncio.create_task(live.service(deleter, "r6-cascade").delete(collection.id))
        try:
            await asyncio.wait_for(entered.wait(), 30)
            async with live.factory() as uploader:
                await bind_tenant(uploader, live.principal.tenant_id)
                document = await DocumentRepository(uploader, live.principal.tenant_id).create(
                    owner_id=live.principal.user_id,
                    collection_id=collection.id,
                    filename="r6.txt",
                    mime_type="text/plain",
                    size_bytes=1,
                    storage_key=f"{live.principal.tenant_id}/{'a' * 64}/r6.txt",
                    acl_enforced=False,
                    status=DocumentStatus.READY,
                )
                await uploader.commit()
            resume.set()
            assert await asyncio.wait_for(task, 30) is True
            # Even when the caller rolls back, its provisional cascade metadata
            # must describe the actual locked documents rather than a stale list.
            provisional = await AuditEventRepository(
                deleter, live.principal.tenant_id
            ).list_recent()
            event = next(row for row in provisional if row.action == "collection.deleted")
            assert event.metadata == {"document_count": 1}
            assert [row.resource_id for row in provisional if row.action == "document.deleted"] == [
                str(document.id)
            ]
            if commit:
                await deleter.commit()
            else:
                await deleter.rollback()
        finally:
            resume.set()
            await asyncio.gather(task, return_exceptions=True)
    async with live.factory() as read:
        await bind_tenant(read, live.principal.tenant_id)
        assert (
            await DocumentRepository(read, live.principal.tenant_id).get(document.id) is None
        ) == commit
        rows = await AuditEventRepository(read, live.principal.tenant_id).list_recent()
        assert len(rows) == (2 if commit else 0)
        if commit:
            assert sorted(row.action for row in rows) == ["collection.deleted", "document.deleted"]


async def test_full_width_inet_first_insert_and_lost_ack(live, monkeypatch):
    """R5-003: PostgreSQL drops /32 and /128 but retains narrower host/prefixes."""
    real_commit = AsyncSession.commit
    lost = False
    failures = 0

    async def lose_ack(session):
        nonlocal lost, failures
        await real_commit(session)
        if lost and session.bind is live.transactions._engine:
            lost = False
            failures += 1
            raise TimeoutError("R6 lost acknowledgement")

    monkeypatch.setattr(AsyncSession, "commit", lose_ack)
    expected = []
    for source_ip, canonical in (
        ("203.0.113.79/32", "203.0.113.79"),
        ("2001:db8::1/128", "2001:db8::1"),
        ("10.1.2.3/8", "10.1.2.3/8"),
        ("2001:db8::1/64", "2001:db8::1/64"),
    ):
        for ambiguous in (False, True):
            async with live.factory() as caller:
                await bind_tenant(caller, live.principal.tenant_id)
                pending = await CollectionRepository(caller, live.principal.tenant_id).create(
                    owner_id=live.principal.user_id, name="inet caller rollback"
                )
                lost = ambiguous
                event = await PermissionDeniedRecorder(
                    live.transactions, tenant_id=live.principal.tenant_id, request_session=caller
                ).emit(
                    actor=AuditActor.user(live.principal.user_id),
                    resource_type="document",
                    resource_id=str(uuid.uuid4()),
                    attempted_action="document.read",
                    reason="not_visible",
                    request_id=f"r6-inet-{uuid.uuid4()}",
                    source_ip=source_ip,
                )
                await caller.rollback()
            expected.append((event, canonical, pending.id))
    assert failures == 4
    async with live.factory() as read:
        await bind_tenant(read, live.principal.tenant_id)
        rows = await AuditEventRepository(read, live.principal.tenant_id).list_recent()
        assert len(rows) == len(expected) == 8
        for event, canonical, pending_id in expected:
            stored = [row for row in rows if row.id == event.id]
            assert len(stored) == 1
            assert str(stored[0].source_ip) == canonical
            assert stored[0].actor_id == live.principal.user_id
            assert (
                await CollectionRepository(read, live.principal.tenant_id).get(pending_id) is None
            )
        first = expected[0][0]
        with pytest.raises(RuntimeError, match="different canonical payload"):
            await AuditEventRepository(read, live.principal.tenant_id).record(
                event_id=first.id,
                action=first.action,
                actor_id=first.actor_id,
                resource_type=first.resource_type,
                resource_id=first.resource_id,
                outcome=AuditOutcome.DENIED,
                request_id=first.request_id,
                source_ip="203.0.113.80/32",
                metadata=first.metadata,
            )


async def test_collection_parent_lock_blocks_upload_after_snapshot(live, monkeypatch):
    """R5-001: a late FK insert is blocked until deletion commits, then fails."""
    collection = await _collection(live)
    snapshotted, resume, uploading = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_refresh = AsyncSession.refresh
    async with live.factory() as deleter, live.factory() as uploader, live.factory() as observer:
        await bind_tenant(deleter, live.principal.tenant_id)
        await bind_tenant(uploader, live.principal.tenant_id)
        uploader_pid = (await uploader.execute(text("SELECT pg_backend_pid()"))).scalar_one()

        async def pause_refresh(session, *args, **kwargs):
            if session is deleter and kwargs.get("attribute_names") == ["documents"]:
                snapshotted.set()
                await resume.wait()
            return await real_refresh(session, *args, **kwargs)

        async def upload():
            uploading.set()
            try:
                await DocumentRepository(uploader, live.principal.tenant_id).create(
                    owner_id=live.principal.user_id,
                    collection_id=collection.id,
                    filename="late.txt",
                    mime_type="text/plain",
                    size_bytes=1,
                    storage_key=f"{live.principal.tenant_id}/{'b' * 64}/late.txt",
                    acl_enforced=False,
                    status=DocumentStatus.READY,
                )
                await uploader.commit()
                return "committed"
            except IntegrityError:
                await uploader.rollback()
                return "parent_deleted"

        async def blocked_in_database(task):
            while True:
                blocked = (
                    await observer.execute(
                        text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                        {"pid": uploader_pid},
                    )
                ).scalar_one()
                if blocked:
                    return
                assert not task.done(), "upload escaped the parent lock after the snapshot"

        monkeypatch.setattr(AsyncSession, "refresh", pause_refresh)
        deletion = asyncio.create_task(live.service(deleter, "r6-locked").delete(collection.id))
        insertion = None
        try:
            await asyncio.wait_for(snapshotted.wait(), 30)
            insertion = asyncio.create_task(upload())
            await uploading.wait()
            await asyncio.wait_for(blocked_in_database(insertion), 30)
            resume.set()
            assert await asyncio.wait_for(deletion, 30) is True
            await deleter.commit()
            assert await asyncio.wait_for(insertion, 30) == "parent_deleted"
        finally:
            resume.set()
            await asyncio.gather(deletion, return_exceptions=True)
            await deleter.rollback()
            if insertion is not None:
                await asyncio.gather(insertion, return_exceptions=True)
        await bind_tenant(observer, live.principal.tenant_id)
        events = await AuditEventRepository(observer, live.principal.tenant_id).list_recent()
        assert len(events) == 1
        assert events[0].action == "collection.deleted"
        assert events[0].metadata == {"document_count": 0}


async def test_verified_websocket_denials_persist_with_rls_and_caller_rollback(live, monkeypatch):
    """R5-002: actual ASGI handshake + signed tokens + canonical PostgreSQL recorder."""
    from app.api.deps import get_db_session, get_durable_audit_transactions_dep
    from app.auth import mint_access_token
    from app.core.config import get_settings
    from app.main import create_app
    from app.realtime.backplane import InMemoryBackplane, StreamOwner

    application = create_app()
    application.dependency_overrides[get_durable_audit_transactions_dep] = lambda: live.transactions
    backplane = InMemoryBackplane()
    monkeypatch.setattr("app.realtime.chat_ws.get_backplane", lambda: backplane)
    async with live.factory() as seed:
        await bind_tenant(seed, live.principal.tenant_id)
        colleague = await UserRepository(seed, live.principal.tenant_id).create(
            email="r6-colleague@audit.invalid", password_hash="not-a-password", roles=[Role.MEMBER]
        )
        await seed.commit()
    await backplane.bind_owner(
        "r6-private", StreamOwner(owner_id=colleague.id, tenant_id=live.principal.tenant_id)
    )
    await backplane.bind_owner(
        "r6-foreign",
        StreamOwner(owner_id=live.principal.user_id, tenant_id=live.principal.tenant_id),
    )
    pending_ids = []
    for stream_id, principal in (
        ("r6-private", live.principal),
        ("r6-foreign", live.foreign),
        ("r6-unknown", live.principal),
    ):
        async with live.factory() as caller:
            await bind_tenant(caller, principal.tenant_id)
            pending = await CollectionRepository(caller, principal.tenant_id).create(
                owner_id=principal.user_id, name="ws caller rollback"
            )
            pending_ids.append((pending.id, principal))

            async def caller_session():
                yield caller

            application.dependency_overrides[get_db_session] = caller_session
            token = mint_access_token(principal, get_settings()).token
            sent = []

            async def receive():
                return {"type": "websocket.connect"}

            async def send(message, captured=sent):
                captured.append(message)

            await application(
                {
                    "type": "websocket",
                    "asgi": {"version": "3.0"},
                    "scheme": "ws",
                    "path": f"/ws/chat/{stream_id}",
                    "root_path": "",
                    "query_string": f"access_token={token}&secret=do-not-store".encode(),
                    "headers": [(b"x-request-id", stream_id.encode())],
                    "client": ("203.0.113.79", 40000),
                    "server": ("test", 80),
                    "subprotocols": [],
                },
                receive,
                send,
            )
            assert [message["type"] for message in sent] == ["websocket.close"]
            assert sent[0]["code"] == 1008
            await caller.rollback()
    for principal, count in ((live.principal, 2), (live.foreign, 1)):
        async with live.factory() as read:
            await bind_tenant(read, principal.tenant_id)
            # Raw ORM reads prove the RLS backstop, without repository predicates.
            rows = (await read.execute(select(models.AuditEvent))).scalars().all()
            assert len(rows) == count
            assert all(
                row.tenant_id == principal.tenant_id and row.actor_id == principal.user_id
                for row in rows
            )
            assert all(
                row.action == "permission.denied" and row.resource_type == "chat_stream"
                for row in rows
            )
            assert all(
                str(row.source_ip) == "203.0.113.79" and row.source_origin == "client"
                for row in rows
            )
            assert all(
                row.event_metadata
                == {"attempted_action": "chat.stream.subscribe", "reason": "not_visible"}
                for row in rows
            )
            assert all(row.request_id == row.resource_id for row in rows)
            for pending_id, pending_principal in pending_ids:
                if pending_principal.tenant_id == principal.tenant_id:
                    assert (
                        await CollectionRepository(read, principal.tenant_id).get(pending_id)
                        is None
                    )
