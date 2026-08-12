"""Disposable-Postgres proof for durable denial isolation (#579, R1/R2).

Opt in with ``AUDIT_DENIAL_LIVE_DATABASE_URL``.  The test creates and migrates a
random database, exercises both a size-one request pool and a fully occupied
concurrent request pool against separately owned audit capacity, proves caller
rollback/exactly-once persistence, reconciles a lost COMMIT acknowledgement,
and checks cross-tenant attribution/idempotency under the real tenant GUC/RLS
backstop and a least-privilege application role.
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
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.errors import ForbiddenError, NotFoundError
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
from app.domain.audit import AuditActor
from app.domain.entities import (
    AssistantStatus,
    AuditEvent,
    AutonomyLevel,
    KnowledgeScope,
    Role,
    RunTrigger,
)
from app.services.admin_service import AdminService
from app.services.artifacts_service import ArtifactsService
from app.services.assistants_service import config_from_assistant
from app.services.audit import AuditSink, PermissionDeniedContext, PermissionDeniedRecorder
from app.services.document_service import DocumentService
from app.services.groups_service import GroupsService
from app.services.llm_providers_service import build_llm_provider_service
from app.services.mcp_servers_service import build_mcp_servers_service
from app.services.run_delivery_service import RunDeliveryService
from app.services.runs_service import RunsReadService
from app.services.saved_searches_service import SavedSearchService
from app.services.sources_service import SourcesService

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_BASE_URL = os.environ.get("AUDIT_DENIAL_LIVE_DATABASE_URL")


def _swap_db(url: str, dbname: str) -> str:
    return urlunparse(urlparse(url)._replace(path=f"/{dbname}"))


@pytest.mark.skipif(
    _BASE_URL is None,
    reason="Set AUDIT_DENIAL_LIVE_DATABASE_URL for the targeted disposable-Postgres proof.",
)
async def test_durable_denial_pool_isolation_concurrency_and_cross_tenant_rls_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 isolation and R2 idempotency hold on migrated least-privilege PostgreSQL."""
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
    providers: list[DurableAuditTransactions] = []
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
                "documents",
                "chunks",
                "grants",
                "groups",
                "group_members",
                "mcp_servers",
                "sources",
                "artifacts",
                "saved_searches",
                "assistants",
                "assistant_versions",
                "runs",
                "run_steps",
                "run_deliveries",
                "llm_providers",
                "secrets",
                "audit_events",
            ):
                await owner_connection.execute(
                    text(f"GRANT SELECT, INSERT ON TABLE {table} " f'TO "{app_role}"')
                )
            await owner_connection.execute(text(f'GRANT UPDATE ON TABLE users TO "{app_role}"'))

        audit_engine = create_async_engine(
            app_url,
            pool_size=2,
            max_overflow=0,
            pool_timeout=2,
        )
        engines.append(audit_engine)
        transactions = DurableAuditTransactions(audit_engine, operation_timeout_seconds=5)
        providers.append(transactions)

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
                actor=AuditActor.user(actor_a.id),
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
                    actor=AuditActor.user(actor_a.id),
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

        # R3-002: every confirmed user-reachable family executes its service-local
        # direct-resource guard through the same durable provider under the real
        # least-privilege role.  The three families with their own role checks also
        # prove direct-service INV-5; router-level `require_roles` remains a separate
        # sole owner and therefore cannot duplicate these calls.
        family_expectations: dict[str, tuple[str, str, str, str, tuple[str, ...]]] = {}
        async with concurrent_factory() as family_caller:
            await bind_tenant(family_caller, tenant_a.id)
            live_settings = get_settings()
            action_sink = AuditSink(AuditEventRepository(family_caller, tenant_a.id))

            def _family_context(
                family: str,
                *,
                resource_type: str,
                resource_id: str,
                attempted_action: str,
                reason: str = "not_visible",
                required_roles: tuple[str, ...] = (),
            ) -> PermissionDeniedContext:
                request_id = f"req-family-{family}-{uuid.uuid4()}"
                family_expectations[request_id] = (
                    resource_type,
                    resource_id,
                    attempted_action,
                    reason,
                    required_roles,
                )
                return PermissionDeniedContext(
                    PermissionDeniedRecorder(
                        transactions,
                        tenant_id=tenant_a.id,
                        request_session=family_caller,
                    ),
                    actor=AuditActor.user(actor_a.id),
                    request_id=request_id,
                    source_ip="203.0.113.83",
                )

            document_id = uuid.uuid4()
            document_denials = _family_context(
                "document",
                resource_type="document",
                resource_id=str(document_id),
                attempted_action="document.read",
            )
            documents = DocumentService(
                family_caller,
                tenant_id=tenant_a.id,
                owner_id=actor_a.id,
                object_store=object(),  # type: ignore[arg-type] -- guard exits first
                audit=action_sink,
                denials=document_denials,
                request_id=document_denials.request_id,
                source_ip=document_denials.source_ip,
                upload_allowed_content_types=frozenset({"text/plain"}),
                max_upload_bytes=1024,
            )
            assert await documents.get(document_id) is None

            mcp_id = uuid.uuid4()
            mcp_denials = _family_context(
                "mcp",
                resource_type="mcp_server",
                resource_id=str(mcp_id),
                attempted_action="mcp_server.read",
            )
            mcp = build_mcp_servers_service(
                family_caller,
                settings=live_settings,
                tenant_id=tenant_a.id,
                owner_id=actor_a.id,
                roles=(Role.MEMBER,),
                audit=action_sink,
                denials=mcp_denials,
                request_id=mcp_denials.request_id,
                source_ip=mcp_denials.source_ip,
            )
            assert await mcp.get(mcp_id) is None

            source_id = uuid.uuid4()
            source_denials = _family_context(
                "source",
                resource_type="source",
                resource_id=str(source_id),
                attempted_action="source.sync",
            )
            sources = SourcesService(
                family_caller,
                tenant_id=tenant_a.id,
                owner_id=actor_a.id,
                roles=(Role.MEMBER,),
                object_store=object(),  # type: ignore[arg-type] -- guard exits first
                audit=action_sink,
                denials=source_denials,
                request_id=source_denials.request_id,
                source_ip=source_denials.source_ip,
            )
            assert await sources.resync(source_id) is None

            artifact_id = uuid.uuid4()
            artifact_denials = _family_context(
                "artifact",
                resource_type="artifact",
                resource_id=str(artifact_id),
                attempted_action="artifact.read",
            )
            artifacts = ArtifactsService(
                family_caller,
                tenant_id=tenant_a.id,
                owner_id=actor_a.id,
                object_store=object(),  # type: ignore[arg-type] -- guard exits first
                audit=action_sink,
                denials=artifact_denials,
                request_id=artifact_denials.request_id,
                source_ip=artifact_denials.source_ip,
                artifact_allowed_content_types=frozenset({"text/plain"}),
                max_artifact_bytes=1024,
            )
            assert await artifacts.get_artifact(artifact_id) is None

            saved_search_id = uuid.uuid4()
            saved_denials = _family_context(
                "saved-search",
                resource_type="saved_search",
                resource_id=str(saved_search_id),
                attempted_action="saved_search.read",
            )
            saved_searches = SavedSearchService(
                family_caller,
                tenant_id=tenant_a.id,
                owner_id=actor_a.id,
                denials=saved_denials,
            )
            assert await saved_searches.get(saved_search_id) is None

            delivery_id = uuid.uuid4()
            delivery_denials = _family_context(
                "run-delivery",
                resource_type="run_delivery",
                resource_id=str(delivery_id),
                attempted_action="run.delivery.read",
            )
            deliveries = RunDeliveryService(
                family_caller,
                tenant_id=tenant_a.id,
                recipient_id=actor_a.id,
                audit=action_sink,
                denials=delivery_denials,
                request_id=delivery_denials.request_id,
                source_ip=delivery_denials.source_ip,
            )
            with pytest.raises(NotFoundError):
                await deliveries.mark_read(delivery_id)

            group_id = uuid.uuid4()
            group_denials = _family_context(
                "group",
                resource_type="group",
                resource_id=str(group_id),
                attempted_action="group.read",
            )
            groups = GroupsService(
                family_caller,
                tenant_id=tenant_a.id,
                actor_id=actor_a.id,
                roles=(Role.ADMIN,),
                audit=action_sink,
                denials=group_denials,
                request_id=group_denials.request_id,
                source_ip=group_denials.source_ip,
            )
            with pytest.raises(NotFoundError):
                await groups.get_group(group_id)

            provider_id = uuid.uuid4()
            provider_denials = _family_context(
                "llm-provider",
                resource_type="llm_provider",
                resource_id=str(provider_id),
                attempted_action="llm_provider.update",
            )
            providers_service = build_llm_provider_service(
                family_caller,
                settings=live_settings,
                tenant_id=tenant_a.id,
                owner_id=actor_a.id,
                roles=(Role.ADMIN,),
                audit=action_sink,
                denials=provider_denials,
                request_id=provider_denials.request_id,
                source_ip=provider_denials.source_ip,
            )
            assert await providers_service.update(provider_id, name="still hidden") is None

            member_id = uuid.uuid4()
            member_denials = _family_context(
                "member-attestation",
                resource_type="user",
                resource_id=str(member_id),
                attempted_action="user.identity.attest",
            )
            admin_service = AdminService(
                family_caller,
                tenant_id=tenant_a.id,
                settings=live_settings,
            )
            assert (
                await admin_service.attest_member_identity(
                    member_id,
                    denials=member_denials,
                )
                is None
            )

            sources_role_denials = _family_context(
                "source-role",
                resource_type="source",
                resource_id="new",
                attempted_action="source.create",
                reason="managed_source_create",
                required_roles=(Role.ADMIN.value,),
            )
            sources_role = SourcesService(
                family_caller,
                tenant_id=tenant_a.id,
                owner_id=actor_a.id,
                roles=(Role.MEMBER,),
                object_store=object(),  # type: ignore[arg-type] -- role guard exits first
                audit=action_sink,
                denials=sources_role_denials,
                request_id=sources_role_denials.request_id,
                source_ip=sources_role_denials.source_ip,
            )
            with pytest.raises(ForbiddenError):
                await sources_role.add(source_type="gdrive")

            group_role_id = uuid.uuid4()
            group_role_denials = _family_context(
                "group-role",
                resource_type="group",
                resource_id=str(group_role_id),
                attempted_action="group.read",
                reason="missing_required_role",
                required_roles=(Role.ADMIN.value,),
            )
            groups_role = GroupsService(
                family_caller,
                tenant_id=tenant_a.id,
                actor_id=actor_a.id,
                roles=(Role.MEMBER,),
                audit=action_sink,
                denials=group_role_denials,
                request_id=group_role_denials.request_id,
                source_ip=group_role_denials.source_ip,
            )
            with pytest.raises(ForbiddenError):
                await groups_role.get_group(group_role_id)

            provider_role_id = uuid.uuid4()
            provider_role_denials = _family_context(
                "llm-provider-role",
                resource_type="llm_provider",
                resource_id=str(provider_role_id),
                attempted_action="llm_provider.update",
                reason="missing_required_role",
                required_roles=(Role.ADMIN.value,),
            )
            providers_role = build_llm_provider_service(
                family_caller,
                settings=live_settings,
                tenant_id=tenant_a.id,
                owner_id=actor_a.id,
                roles=(Role.MEMBER,),
                audit=action_sink,
                denials=provider_role_denials,
                request_id=provider_role_denials.request_id,
                source_ip=provider_role_denials.source_ip,
            )
            with pytest.raises(ForbiddenError):
                await providers_role.update(provider_role_id, name="forbidden")

            await family_caller.rollback()

        # R2-003: a real PostgreSQL COMMIT succeeds, but its acknowledgement is
        # withheld until the provider deadline cancels the await. Reconciliation
        # uses a fresh tenant-bound session, returns the already-durable row, and
        # an immediate second denial proves the owned pool was not poisoned.
        ambiguous_engine = create_async_engine(
            app_url,
            pool_size=2,
            max_overflow=0,
            pool_timeout=2,
        )
        engines.append(ambiguous_engine)
        ambiguous_transactions = DurableAuditTransactions(
            ambiguous_engine,
            operation_timeout_seconds=0.15,
        )
        providers.append(ambiguous_transactions)
        real_commit = AsyncSession.commit
        lost_ack_commits = 0
        cancelled_after_commit = 0
        lose_next_ack = False

        async def _commit_then_lose_ack(audit_session: AsyncSession) -> None:
            nonlocal cancelled_after_commit, lose_next_ack, lost_ack_commits
            await real_commit(audit_session)
            if audit_session.bind is ambiguous_engine and lose_next_ack:
                lose_next_ack = False
                lost_ack_commits += 1
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled_after_commit += 1

        monkeypatch.setattr(AsyncSession, "commit", _commit_then_lose_ack)
        ambiguous_request_id = f"req-commit-lost-ack-{uuid.uuid4()}"
        async with concurrent_factory() as caller:
            await bind_tenant(caller, tenant_a.id)
            pending_ambiguous = await CollectionRepository(caller, tenant_a.id).create(
                owner_id=actor_a.id,
                name="ambiguous caller must roll back",
            )
            await caller.flush()
            lose_next_ack = True
            ambiguous_event = await PermissionDeniedRecorder(
                ambiguous_transactions,
                tenant_id=tenant_a.id,
                request_session=caller,
            ).emit(
                actor=AuditActor.system(),
                resource_type="assistant",
                resource_id=str(assistant.id),
                attempted_action="run.enqueue",
                reason="not_visible",
                request_id=ambiguous_request_id,
                source_ip="system",
            )
            await caller.rollback()

        assert lost_ack_commits == cancelled_after_commit == 1

        # R3-001: force the same lost-COMMIT-ack reconciliation through the two
        # PostgreSQL INET spellings that previously committed and then rejected
        # their own payload.  Canonicalization happens before insert/comparison,
        # so each semantic denial returns the one durable canonical row.
        canonical_lost_ack_events: list[tuple[AuditEvent, str, uuid.UUID]] = []
        pending_canonical: list[uuid.UUID] = []
        for source_ip, canonical_ip in (
            ("2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::1"),
            ("::ffff:192.0.2.128", "::ffff:c000:280"),
        ):
            canonical_request_id = f"req-canonical-lost-ack-{uuid.uuid4()}"
            async with concurrent_factory() as caller:
                await bind_tenant(caller, tenant_a.id)
                pending = await CollectionRepository(caller, tenant_a.id).create(
                    owner_id=actor_a.id,
                    name=f"canonical caller rollback {canonical_ip}",
                )
                pending_canonical.append(pending.id)
                await caller.flush()
                lose_next_ack = True
                event = await PermissionDeniedRecorder(
                    ambiguous_transactions,
                    tenant_id=tenant_a.id,
                    request_session=caller,
                ).emit(
                    actor=AuditActor.user(actor_a.id),
                    resource_type="document",
                    resource_id=str(uuid.uuid4()),
                    attempted_action="document.read",
                    reason="not_visible",
                    request_id=canonical_request_id,
                    source_ip=source_ip,
                )
                await caller.rollback()
            assert event.source_origin == "client"
            assert str(event.source_ip) == canonical_ip
            canonical_lost_ack_events.append((event, canonical_ip, pending.id))

        assert lost_ack_commits == cancelled_after_commit == 3
        recovery_request_id = f"req-after-commit-lost-ack-{uuid.uuid4()}"
        async with concurrent_factory() as caller:
            await bind_tenant(caller, tenant_a.id)
            recovered_capacity_event = await PermissionDeniedRecorder(
                ambiguous_transactions,
                tenant_id=tenant_a.id,
                request_session=caller,
            ).emit(
                actor=AuditActor.user(actor_a.id),
                resource_type="assistant",
                resource_id=str(uuid.uuid4()),
                attempted_action="assistant.read",
                reason="not_visible",
                request_id=recovery_request_id,
                source_ip="203.0.113.81",
            )
            await caller.rollback()

        # The explicit repository storage boundary is safe under actual
        # concurrent same-key attempts: both callers receive the semantic id,
        # while PostgreSQL stores one canonical row.
        concurrent_event_id = uuid.uuid4()
        concurrent_idempotency_request = f"req-idempotent-concurrent-{uuid.uuid4()}"
        ambiguous_factory = async_sessionmaker(
            ambiguous_engine,
            expire_on_commit=False,
            autoflush=False,
        )

        async def _write_same_event(source_ip: str) -> uuid.UUID:
            async with ambiguous_factory() as writer:
                await bind_tenant(writer, tenant_a.id)
                event = await AuditEventRepository(writer, tenant_a.id).record(
                    event_id=concurrent_event_id,
                    action="permission.denied",
                    resource_type="assistant",
                    outcome=ambiguous_event.outcome,
                    actor_id=actor_a.id,
                    resource_id=str(assistant.id),
                    request_id=concurrent_idempotency_request,
                    source_ip=source_ip,
                    metadata={"attempted_action": "assistant.read", "reason": "not_visible"},
                )
                await writer.commit()
                return event.id

        assert await asyncio.gather(
            _write_same_event("2001:0db8:0000:0000:0000:0000:0000:0002"),
            _write_same_event("2001:db8::2"),
        ) == [
            concurrent_event_id,
            concurrent_event_id,
        ]

        # The equality is semantic, not permissive: the same trusted UUID with a
        # genuinely different address is a payload collision and fails closed.
        async with ambiguous_factory() as mismatched_writer:
            await bind_tenant(mismatched_writer, tenant_a.id)
            with pytest.raises(RuntimeError) as mismatch:
                await AuditEventRepository(mismatched_writer, tenant_a.id).record(
                    event_id=concurrent_event_id,
                    action="permission.denied",
                    resource_type="assistant",
                    outcome=ambiguous_event.outcome,
                    actor_id=actor_a.id,
                    resource_id=str(assistant.id),
                    request_id=concurrent_idempotency_request,
                    source_ip="2001:db8::3",
                    metadata={
                        "attempted_action": "assistant.read",
                        "reason": "not_visible",
                    },
                )
            assert "2001:db8::" not in str(mismatch.value)
            await mismatched_writer.rollback()

        # A UUID collision in another tenant remains invisible through both RLS
        # and the repository predicate and can never satisfy reconciliation.
        foreign_collision_id = uuid.uuid4()
        async with ambiguous_factory() as tenant_a_writer:
            await bind_tenant(tenant_a_writer, tenant_a.id)
            await AuditEventRepository(tenant_a_writer, tenant_a.id).record(
                event_id=foreign_collision_id,
                action="permission.denied",
                resource_type="assistant",
                outcome=ambiguous_event.outcome,
                actor_id=None,
                resource_id=str(assistant.id),
                request_id="req-foreign-idempotency-collision",
                source_ip="system",
                metadata={"attempted_action": "run.enqueue", "reason": "not_visible"},
            )
            await tenant_a_writer.commit()
        async with ambiguous_factory() as tenant_b_writer:
            await bind_tenant(tenant_b_writer, tenant_b.id)
            tenant_b_repository = AuditEventRepository(tenant_b_writer, tenant_b.id)
            assert await tenant_b_repository.get(foreign_collision_id) is None
            with pytest.raises(RuntimeError, match="idempotency.*tenant"):
                await tenant_b_repository.record(
                    event_id=foreign_collision_id,
                    action="permission.denied",
                    resource_type="assistant",
                    outcome=ambiguous_event.outcome,
                    actor_id=None,
                    resource_id=str(assistant.id),
                    request_id="req-foreign-idempotency-collision",
                    source_ip="system",
                    metadata={"attempted_action": "run.enqueue", "reason": "not_visible"},
                )
            await tenant_b_writer.rollback()

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
            ).list_recent(limit=200)
            matching_a = [
                event for event in tenant_a_events if event.request_id in expected_request_ids
            ]
            assert len(matching_a) == 4
            assert {event.request_id for event in matching_a} == {
                size_one_request_id,
                *concurrent_request_ids,
            }
            family_events = [
                event for event in tenant_a_events if event.request_id in family_expectations
            ]
            assert len(family_events) == len(family_expectations) == 12
            for event in family_events:
                (
                    resource_type,
                    resource_id,
                    attempted_action,
                    reason,
                    required_roles,
                ) = family_expectations[event.request_id]
                assert event.tenant_id == tenant_a.id
                assert event.actor_id == actor_a.id
                assert event.action == "permission.denied"
                assert event.outcome.value == "denied"
                assert event.resource_type == resource_type
                assert event.resource_id == resource_id
                assert event.source_origin == "client"
                assert str(event.source_ip) == "203.0.113.83"
                assert event.metadata == {
                    "attempted_action": attempted_action,
                    "reason": reason,
                    **({"required_roles": list(required_roles)} if required_roles else {}),
                }
            raw_cross_tenant_a = await tenant_a_readback.execute(
                select(models.AuditEvent).where(
                    models.AuditEvent.request_id == cross_tenant_request_id
                )
            )
            assert raw_cross_tenant_a.scalar_one_or_none() is None

        async with ambiguous_factory() as idempotency_readback:
            await bind_tenant(idempotency_readback, tenant_a.id)
            assert (
                await CollectionRepository(idempotency_readback, tenant_a.id).get(
                    pending_ambiguous.id
                )
                is None
            )
            for pending_id in pending_canonical:
                assert (
                    await CollectionRepository(idempotency_readback, tenant_a.id).get(pending_id)
                    is None
                )
            idempotency_events = await AuditEventRepository(
                idempotency_readback,
                tenant_a.id,
            ).list_recent(limit=50)
            assert sum(event.id == ambiguous_event.id for event in idempotency_events) == 1
            assert sum(event.id == recovered_capacity_event.id for event in idempotency_events) == 1
            assert sum(event.id == concurrent_event_id for event in idempotency_events) == 1
            for canonical_event, canonical_ip, _pending_id in canonical_lost_ack_events:
                assert sum(event.id == canonical_event.id for event in idempotency_events) == 1
                stored_canonical = next(
                    event for event in idempotency_events if event.id == canonical_event.id
                )
                assert stored_canonical.actor_id == actor_a.id
                assert stored_canonical.source_origin == "client"
                assert str(stored_canonical.source_ip) == canonical_ip
                assert stored_canonical.metadata == {
                    "attempted_action": "document.read",
                    "reason": "not_visible",
                }
            stored_concurrent = next(
                event for event in idempotency_events if event.id == concurrent_event_id
            )
            assert stored_concurrent.source_origin == "client"
            assert str(stored_concurrent.source_ip) == "2001:db8::2"
            stored_ambiguous = next(
                event for event in idempotency_events if event.id == ambiguous_event.id
            )
            assert stored_ambiguous.actor_id is None
            assert stored_ambiguous.source_origin == "system"
            assert stored_ambiguous.source_ip is None
            assert stored_ambiguous.request_id == ambiguous_request_id
            assert stored_ambiguous.metadata == {
                "attempted_action": "run.enqueue",
                "reason": "not_visible",
            }
        assert ambiguous_engine.sync_engine.pool.checkedout() == 0

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
        for provider in reversed(providers):
            await provider.dispose()
        for engine in reversed(engines):
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
