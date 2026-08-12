"""Disposable-Postgres proof for durable denial isolation (#579, R1-001/R1-003).

Opt in with ``AUDIT_DENIAL_LIVE_DATABASE_URL``.  The test creates and migrates a
random database, exercises both a size-one request pool and a fully occupied
concurrent request pool against separately owned audit capacity, proves caller
rollback/exactly-once persistence, and checks cross-tenant run attribution under
the real tenant GUC/RLS backstop.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.core.errors import NotFoundError
from app.db import models
from app.db.audit_transactions import DurableAuditTransactions
from app.db.repositories import (
    AssistantRepository,
    AssistantVersionRepository,
    AuditEventRepository,
    CollectionRepository,
    RunRepository,
    TenantRepository,
    UserRepository,
)
from app.db.tenant_context import bind_bypass, bind_tenant
from app.domain.entities import (
    AssistantStatus,
    AutonomyLevel,
    KnowledgeScope,
    Role,
    RunTrigger,
)
from app.services.assistants_service import config_from_assistant
from app.services.audit import AuditSink, PermissionDeniedRecorder
from app.services.runs_service import RunsReadService

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_BASE_URL = os.environ.get("AUDIT_DENIAL_LIVE_DATABASE_URL")


def _swap_db(url: str, dbname: str) -> str:
    return urlunparse(urlparse(url)._replace(path=f"/{dbname}"))


@pytest.mark.skipif(
    _BASE_URL is None,
    reason="Set AUDIT_DENIAL_LIVE_DATABASE_URL for the targeted disposable-Postgres proof.",
)
async def test_durable_denial_pool_isolation_concurrency_and_cross_tenant_rls_live() -> None:
    """All R1-001/R1-003 live invariants hold on migrated PostgreSQL."""
    assert _BASE_URL is not None
    from alembic import command

    from app.core.config import get_settings

    database_name = f"lumen_audit579_{uuid.uuid4().hex[:12]}"
    app_role = f"lumen_audit579_app_{uuid.uuid4().hex[:8]}"
    app_password = "audit_579_test_pw"  # noqa: S105 — throwaway role/database
    admin_url = _swap_db(_BASE_URL, "postgres")
    database_url = _swap_db(_BASE_URL, database_name)
    parsed = urlparse(database_url)
    app_url = urlunparse(
        parsed._replace(netloc=f"{app_role}:{app_password}@{parsed.hostname}:{parsed.port}")
    )
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
            await connection.execute(
                text(
                    f'CREATE ROLE "{app_role}" LOGIN PASSWORD '
                    f"'{app_password}' NOSUPERUSER NOBYPASSRLS"
                )
            )
    finally:
        await admin.dispose()

    prior_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    get_settings.cache_clear()
    engines: list[AsyncEngine] = []
    transactions: DurableAuditTransactions | None = None
    try:
        config = Config(str(_BACKEND_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
        await asyncio.to_thread(command.upgrade, config, "head")

        seed_engine = create_async_engine(database_url)
        engines.append(seed_engine)
        seed_factory = async_sessionmaker(seed_engine, expire_on_commit=False, autoflush=False)
        async with seed_factory() as seed:
            await bind_bypass(seed)
            tenant_a = await TenantRepository(seed).create(name="Acme live")
            tenant_b = await TenantRepository(seed).create(name="Globex live")
            actor_a = await UserRepository(seed, tenant_a.id).create(
                email="alice@audit579.invalid",
                password_hash="disposable-not-a-credential",
                roles=[Role.MEMBER],
            )
            actor_b = await UserRepository(seed, tenant_b.id).create(
                email="carol@audit579.invalid",
                password_hash="disposable-not-a-credential",
                roles=[Role.MEMBER],
            )
            assistant = await AssistantRepository(seed, tenant_a.id).create(
                owner_id=actor_a.id,
                name="Tenant A assistant",
                knowledge_scope=KnowledgeScope.empty(),
                tool_allowlist=(),
                autonomy_level=AutonomyLevel.SUGGEST,
            )
            await AssistantRepository(seed, tenant_a.id).update(
                assistant.id,
                fields={"status": AssistantStatus.PUBLISHED},
            )
            head = await AssistantRepository(seed, tenant_a.id).get(assistant.id)
            assert head is not None
            version = await AssistantVersionRepository(seed, tenant_a.id).add(
                assistant_id=assistant.id,
                version=1,
                author_id=actor_a.id,
                config=config_from_assistant(head),
            )
            run = await RunRepository(seed, tenant_a.id).create(
                owner_id=actor_a.id,
                assistant_id=assistant.id,
                assistant_version_id=version.id,
                trigger=RunTrigger.MANUAL,
                inputs={"prompt": "live"},
            )
            await seed.commit()

        # All request/audit assertions below use the realistic least-privilege
        # role.  The compose owner is a superuser and would bypass RLS even when
        # the migration uses FORCE ROW LEVEL SECURITY.
        async with seed_engine.connect() as owner_connection:
            await owner_connection.execution_options(isolation_level="AUTOCOMMIT")
            for table in (
                "tenants",
                "users",
                "collections",
                "assistants",
                "assistant_versions",
                "runs",
                "run_steps",
                "audit_events",
            ):
                await owner_connection.execute(
                    text(f"GRANT SELECT, INSERT ON TABLE {table} " f'TO "{app_role}"')
                )

        audit_engine = create_async_engine(
            app_url,
            pool_size=2,
            max_overflow=0,
            pool_timeout=2,
        )
        engines.append(audit_engine)
        transactions = DurableAuditTransactions(audit_engine, operation_timeout_seconds=5)

        # Required regression (1): the request owns the sole request-pool slot and
        # has flushed unrelated work.  Durable audit still completes through its
        # owned pool, and rolling the caller back removes only caller work.
        size_one_engine = create_async_engine(
            app_url,
            pool_size=1,
            max_overflow=0,
            pool_timeout=2,
        )
        engines.append(size_one_engine)
        size_one_factory = async_sessionmaker(
            size_one_engine,
            expire_on_commit=False,
            autoflush=False,
        )
        size_one_request_id = f"req-size-one-{uuid.uuid4()}"
        async with size_one_factory() as caller:
            await bind_tenant(caller, tenant_a.id)
            pending_one = await CollectionRepository(caller, tenant_a.id).create(
                owner_id=actor_a.id,
                name="size-one must roll back",
            )
            await caller.flush()
            recorder = PermissionDeniedRecorder(
                transactions,
                tenant_id=tenant_a.id,
                request_session=caller,
            )
            await recorder.emit(
                actor_id=actor_a.id,
                resource_type="assistant",
                resource_id=str(uuid.uuid4()),
                attempted_action="assistant.read",
                reason="not_visible",
                request_id=size_one_request_id,
                source_ip="203.0.113.79",
            )
            await caller.rollback()

        # Required regression (2): fill every one of three request slots before
        # any denial emits.  The fourth, unrelated request queues behind them and
        # still progresses because denials never reacquire the request pool.
        concurrent_engine = create_async_engine(
            app_url,
            pool_size=3,
            max_overflow=0,
            pool_timeout=5,
        )
        engines.append(concurrent_engine)
        concurrent_factory = async_sessionmaker(
            concurrent_engine,
            expire_on_commit=False,
            autoflush=False,
        )
        barrier = asyncio.Barrier(3)
        concurrent_request_ids = [f"req-concurrent-{i}-{uuid.uuid4()}" for i in range(3)]

        async def _deny_at_capacity(ordinal: int) -> uuid.UUID:
            async with concurrent_factory() as caller:
                await bind_tenant(caller, tenant_a.id)
                pending = await CollectionRepository(caller, tenant_a.id).create(
                    owner_id=actor_a.id,
                    name=f"concurrent rollback {ordinal}",
                )
                await caller.flush()
                await barrier.wait()
                recorder = PermissionDeniedRecorder(
                    transactions,
                    tenant_id=tenant_a.id,
                    request_session=caller,
                )
                await recorder.emit(
                    actor_id=actor_a.id,
                    resource_type="assistant",
                    resource_id=str(uuid.uuid4()),
                    attempted_action="assistant.read",
                    reason="not_visible",
                    request_id=concurrent_request_ids[ordinal],
                    source_ip="203.0.113.79",
                )
                await caller.rollback()
                return pending.id

        async def _unrelated_success() -> uuid.UUID:
            async with concurrent_factory() as session:
                await bind_tenant(session, tenant_a.id)
                visible = await UserRepository(session, tenant_a.id).get(actor_a.id)
                assert visible is not None
                return visible.id

        concurrent_results = await asyncio.wait_for(
            asyncio.gather(
                *(_deny_at_capacity(i) for i in range(3)),
                _unrelated_success(),
            ),
            timeout=15,
        )
        pending_concurrent = concurrent_results[:3]
        assert concurrent_results[3] == actor_a.id

        # R1-003: the cross-tenant caller is a real tenant-B actor, not an invalid
        # tenant-A id paired with tenant B.  Real RLS hides tenant A's run, then the
        # denial is attributed exactly once to tenant B / actor B.
        cross_tenant_request_id = f"req-run-cross-tenant-{uuid.uuid4()}"
        async with concurrent_factory() as tenant_b_caller:
            await bind_tenant(tenant_b_caller, tenant_b.id)
            assert await UserRepository(tenant_b_caller, tenant_b.id).get(actor_b.id) is not None
            assert await UserRepository(tenant_b_caller, tenant_b.id).get(actor_a.id) is None
            raw_run = await tenant_b_caller.execute(
                select(models.Run).where(models.Run.id == run.id)
            )
            assert raw_run.scalar_one_or_none() is None  # RLS, not repository filtering
            denials = PermissionDeniedRecorder(
                transactions,
                tenant_id=tenant_b.id,
                request_session=tenant_b_caller,
            )
            service = RunsReadService(
                tenant_b_caller,
                tenant_id=tenant_b.id,
                owner_id=actor_b.id,
                audit=AuditSink(AuditEventRepository(tenant_b_caller, tenant_b.id)),
                denials=denials,
                request_id=cross_tenant_request_id,
                source_ip="203.0.113.80",
            )
            with pytest.raises(NotFoundError):
                await service.get(run.id)
            await tenant_b_caller.rollback()

        audit_factory = async_sessionmaker(audit_engine, expire_on_commit=False, autoflush=False)
        expected_request_ids = {
            size_one_request_id,
            *concurrent_request_ids,
            cross_tenant_request_id,
        }
        async with audit_factory() as tenant_a_readback:
            await bind_tenant(tenant_a_readback, tenant_a.id)
            assert (
                await CollectionRepository(tenant_a_readback, tenant_a.id).get(pending_one.id)
                is None
            )
            for pending_id in pending_concurrent:
                assert (
                    await CollectionRepository(tenant_a_readback, tenant_a.id).get(pending_id)
                    is None
                )
            tenant_a_events = await AuditEventRepository(
                tenant_a_readback, tenant_a.id
            ).list_recent(limit=20)
            matching_a = [
                event for event in tenant_a_events if event.request_id in expected_request_ids
            ]
            assert len(matching_a) == 4
            assert {event.request_id for event in matching_a} == {
                size_one_request_id,
                *concurrent_request_ids,
            }
            raw_cross_tenant_a = await tenant_a_readback.execute(
                select(models.AuditEvent).where(
                    models.AuditEvent.request_id == cross_tenant_request_id
                )
            )
            assert raw_cross_tenant_a.scalar_one_or_none() is None

        async with audit_factory() as tenant_b_readback:
            await bind_tenant(tenant_b_readback, tenant_b.id)
            raw_cross_tenant_b = await tenant_b_readback.execute(
                select(models.AuditEvent).where(
                    models.AuditEvent.request_id == cross_tenant_request_id
                )
            )
            cross_event = raw_cross_tenant_b.scalar_one()
            assert cross_event.tenant_id == tenant_b.id
            assert cross_event.actor_id == actor_b.id
            assert cross_event.action == "permission.denied"
            assert cross_event.outcome == "denied"
            assert cross_event.event_metadata == {
                "attempted_action": "run.read",
                "reason": "not_visible",
            }
        async with audit_engine.connect() as role_check:
            assert (
                await role_check.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            ).one() == (False, False)
    finally:
        if transactions is not None:
            await transactions.dispose()
        for engine in reversed(engines):
            if transactions is None or engine is not transactions.engine:
                await engine.dispose()
        if prior_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior_url
        get_settings.cache_clear()
        admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as connection:
                await connection.execute(
                    text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
                )
                await connection.execute(text(f'DROP ROLE IF EXISTS "{app_role}"'))
        finally:
            await admin.dispose()
