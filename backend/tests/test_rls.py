"""Row-level-security backstop tests — INV-1 the point of #17 (spec 0004 §2.1).

Two layers, both offline-safe:

* **Offline unit** — the ``app.db.tenant_context`` helpers are a no-op on a
  non-Postgres engine (the in-memory SQLite the rest of the suite uses), so RLS
  wiring never breaks the offline tests. Runs everywhere.

* **Live Postgres** (skipped when PG is unreachable, mirroring
  ``test_db_migration.py``) — the behavioural proof. Against a **disposable
  throwaway database** (never the app/CI DB — the #70 lesson) with the real
  migrations applied (so RLS is ENABLED+FORCED with the policies), it asserts:

  - **fail-closed, no GUC:** a tenant-scoped query with **no** ``app.tenant_id``
    set returns ZERO rows (and a write is rejected) — absence of a grant is
    denial (spec 0004 §1);
  - **cross-tenant read blocked even without a predicate:** with the GUC bound to
    tenant A, a raw ``SELECT`` carrying **no** ``tenant_id`` predicate (the
    repository predicate deliberately bypassed) returns only A's rows — the RLS
    backstop is what makes a *forgotten* predicate safe (INV-1);
  - **cross-tenant write blocked:** with the GUC bound to A, inserting a row whose
    ``tenant_id`` is B is rejected by the policy ``WITH CHECK``;
  - **positive:** with the GUC bound to A, A's own rows read back normally — RLS
    permits exactly the bound tenant.

The live legs run the migrations as the connecting role (which becomes the table
owner); ``FORCE ROW LEVEL SECURITY`` is what makes the policy bind that owner —
the test would pass spuriously without FORCE, so it is the real assertion.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.tenant_context import BYPASS_SENTINEL, bind_bypass, bind_tenant

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

# ---------------------------------------------------------------------------
# Offline: the GUC helpers no-op on a non-Postgres engine (the SQLite suite).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sqlite_session() -> AsyncIterator[AsyncSession]:
    """An in-memory SQLite session (no RLS) — proves the helpers no-op here."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as sess:
            yield sess
    finally:
        await engine.dispose()


async def test_bind_tenant_is_noop_on_sqlite(sqlite_session: AsyncSession) -> None:
    """``bind_tenant`` must not error / must change nothing on SQLite.

    RLS and ``set_config`` are Postgres-only; on the offline SQLite engine the
    helper returns without touching the connection, so the rest of the suite is
    unaffected by the #17 wiring. A plain ``SELECT 1`` still works afterwards.
    """
    await bind_tenant(sqlite_session, uuid.uuid4())
    await bind_bypass(sqlite_session)
    result = await sqlite_session.execute(text("SELECT 1"))
    assert result.scalar_one() == 1


# ---------------------------------------------------------------------------
# Live Postgres: the RLS behavioural proof. Skipped when PG is unreachable.
# ---------------------------------------------------------------------------

_PG_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://lumen:lumen_local_dev@localhost:47182/lumen"
)


def _pg_reachable(url: str) -> bool:
    parsed = urlparse(url.replace("postgresql+asyncpg", "postgresql"))
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _swap_db(url: str, dbname: str) -> str:
    return urlunparse(urlparse(url)._replace(path=f"/{dbname}"))


_live = pytest.mark.skipif(
    not _pg_reachable(_PG_URL),
    reason=f"Postgres not reachable for {_PG_URL}; live RLS test skipped (offline-safe).",
)

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _alembic_config():  # type: ignore[no-untyped-def]
    from alembic.config import Config

    cfg = Config(os.path.join(_BACKEND_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_BACKEND_ROOT, "alembic"))
    return cfg


@_live
async def test_rls_backstop_isolates_tenants_on_postgres() -> None:
    """Migrated Postgres enforces INV-1 even with the repo predicate bypassed.

    Creates a disposable DB, runs ``alembic upgrade head`` (so RLS is applied),
    seeds two tenants A and B each with a collection (under the bypass GUC), then
    asserts the four RLS invariants above. The temp DB is dropped in teardown.

    **The RLS-sensitive assertions connect as a dedicated NON-superuser app role**
    created in the throwaway DB, because a superuser / ``BYPASSRLS`` role ignores
    row-level security entirely — ``FORCE ROW LEVEL SECURITY`` binds the table
    *owner* but not a superuser. The local compose ``lumen`` role happens to be a
    superuser, so testing as it would prove nothing. This mirrors the deployment
    requirement: **the application must connect as a least-privilege, non-BYPASSRLS
    role** for the backstop to bite (see the PR notes / residual risk). The seed
    and migrations run as the owner; only the policy checks run as the app role.
    """
    import asyncio

    from alembic import command

    tmp_db = f"lumen_rlstest_{uuid.uuid4().hex[:12]}"
    app_role = f"lumen_rls_app_{uuid.uuid4().hex[:8]}"
    app_pw = "rls_test_pw"  # noqa: S105 — throwaway role in a throwaway DB
    admin_url = _swap_db(_PG_URL, "postgres")
    tmp_url = _swap_db(_PG_URL, tmp_db)
    # The app-role DSN: same host/db, swapped credentials. asyncpg DSN form.
    parsed = urlparse(tmp_url)
    app_url = urlunparse(
        parsed._replace(netloc=f"{app_role}:{app_pw}@{parsed.hostname}:{parsed.port}")
    )

    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{tmp_db}"'))
            # A NON-superuser, NON-bypassrls login role — the realistic app role.
            # CREATE ROLE is DDL: the password is a literal (a fixed throwaway
            # constant in a throwaway DB — no bound param, no injection surface).
            create_role = (
                f'CREATE ROLE "{app_role}" LOGIN ' f"PASSWORD '{app_pw}' NOSUPERUSER NOBYPASSRLS"
            )
            await conn.execute(text(create_role))
    finally:
        await admin.dispose()

    orig = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = tmp_url
    from app.core.config import get_settings

    get_settings.cache_clear()
    engine = create_async_engine(tmp_url)  # owner connection (runs the seed)
    app_engine = create_async_engine(app_url)  # least-privilege app connection
    try:
        # env.py spins up its own event loop (asyncio.run); run alembic off-thread.
        await asyncio.to_thread(command.upgrade, _alembic_config(), "head")

        # Grant the app role just enough to exercise the scoped tables. RLS still
        # gates *which rows* it sees; the grants only let it issue the statements.
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            for tbl in ("tenants", "users", "collections"):
                await conn.execute(
                    text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {tbl} TO "{app_role}"')
                )

        tenant_a = uuid.uuid4()
        tenant_b = uuid.uuid4()
        coll_a = uuid.uuid4()
        coll_b = uuid.uuid4()
        user_a = uuid.uuid4()
        user_b = uuid.uuid4()

        owner_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        app_factory = async_sessionmaker(bind=app_engine, expire_on_commit=False)

        # --- seed both tenants under the bypass sentinel (system path) --------
        async with owner_factory() as sess:
            await bind_bypass(sess)
            for tid, uid, cid in ((tenant_a, user_a, coll_a), (tenant_b, user_b, coll_b)):
                await sess.execute(
                    text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
                    {"id": tid, "name": f"Tenant {tid}"},
                )
                await sess.execute(
                    text(
                        "INSERT INTO users (id, tenant_id, email, password_hash, roles) "
                        "VALUES (:id, :tid, :email, 'h', ARRAY['member'])"
                    ),
                    {"id": uid, "tid": tid, "email": f"u-{uid}@t.test"},
                )
                await sess.execute(
                    text(
                        "INSERT INTO collections (id, tenant_id, owner_id, name) "
                        "VALUES (:id, :tid, :oid, 'C')"
                    ),
                    {"id": cid, "tid": tid, "oid": uid},
                )
            await sess.commit()

        # --- fail-closed: NO GUC set → a scoped query returns ZERO rows -------
        async with app_factory() as sess:
            rows = (await sess.execute(text("SELECT id FROM collections"))).scalars().all()
            assert rows == [], "with no app.tenant_id GUC, RLS must return no rows (fail closed)"

        # --- bound to A, NO predicate → only A's rows (backstop for INV-1) ----
        async with app_factory() as sess:
            await bind_tenant(sess, tenant_a)
            # Deliberately no `WHERE tenant_id = ...` — the repository predicate
            # is what we are *bypassing*; RLS alone must isolate.
            rows = (await sess.execute(text("SELECT id, tenant_id FROM collections"))).all()
            ids = {r[0] for r in rows}
            assert ids == {coll_a}, "RLS must hide tenant B's rows from tenant A"
            assert all(r[1] == tenant_a for r in rows)

        # --- bound to A, cross-tenant WRITE → rejected by WITH CHECK ----------
        async with app_factory() as sess:
            await bind_tenant(sess, tenant_a)
            with pytest.raises(Exception):  # noqa: B017,PT011 — any DB policy error
                await sess.execute(
                    text(
                        "INSERT INTO collections (id, tenant_id, owner_id, name) "
                        "VALUES (:id, :tid, :oid, 'X')"
                    ),
                    {"id": uuid.uuid4(), "tid": tenant_b, "oid": user_b},
                )
                await sess.flush()
            await sess.rollback()

        # --- positive: bound to A, A's own row reads back normally ------------
        async with app_factory() as sess:
            await bind_tenant(sess, tenant_a)
            row = (
                await sess.execute(
                    text("SELECT id FROM collections WHERE id = :id"), {"id": coll_a}
                )
            ).scalar_one_or_none()
            assert row == coll_a

        # --- the bypass sentinel can never equal a real tenant_id (a uuid) ----
        assert BYPASS_SENTINEL == "bypass"
        # Sanity: prove the assertions above actually ran under RLS — the app role
        # is neither superuser nor bypassrls (otherwise the test is vacuous).
        async with app_engine.connect() as conn:
            attrs = (
                await conn.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            ).one()
            assert attrs == (False, False), "app role must be NON-superuser, NON-bypassrls"
    finally:
        await engine.dispose()
        await app_engine.dispose()
        if orig is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = orig
        get_settings.cache_clear()
        admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{tmp_db}" WITH (FORCE)'))
                await conn.execute(text(f'DROP ROLE IF EXISTS "{app_role}"'))
        finally:
            await admin.dispose()


@_live
async def test_parallel_call_scopes_keep_rls_isolation_on_postgres() -> None:
    """#412 / ADR-0016 §5 "parallel RLS isolation": concurrent per-call scopes
    stay tenant-isolated under real Postgres RLS.

    The concurrent tool executor opens one fresh session per call and binds the
    tenant GUC on each (``chat_runtime._call_scope``). This is the live proof
    that the mechanism is sound under REAL parallelism: four call-scope-shaped
    workers (two bound to tenant A, two to B) start together behind a barrier,
    then each runs the predicate-free ``SELECT`` (repository predicate
    deliberately bypassed) — every worker must see exactly its own tenant's
    rows. ``SET LOCAL``-semantics binding is per transaction, so concurrent
    sessions on pooled connections must never leak a GUC across workers.
    Connects as a NON-superuser, NON-bypassrls app role (a superuser would make
    the test vacuous — see the test above).
    """
    import asyncio

    from alembic import command

    tmp_db = f"lumen_rls412_{uuid.uuid4().hex[:12]}"
    app_role = f"lumen_rls412_app_{uuid.uuid4().hex[:8]}"
    app_pw = "rls_test_pw"  # noqa: S105 — throwaway role in a throwaway DB
    admin_url = _swap_db(_PG_URL, "postgres")
    tmp_url = _swap_db(_PG_URL, tmp_db)
    parsed = urlparse(tmp_url)
    app_url = urlunparse(
        parsed._replace(netloc=f"{app_role}:{app_pw}@{parsed.hostname}:{parsed.port}")
    )

    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{tmp_db}"'))
            create_role = (
                f'CREATE ROLE "{app_role}" LOGIN ' f"PASSWORD '{app_pw}' NOSUPERUSER NOBYPASSRLS"
            )
            await conn.execute(text(create_role))
    finally:
        await admin.dispose()

    orig = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = tmp_url
    from app.core.config import get_settings

    get_settings.cache_clear()
    engine = create_async_engine(tmp_url)
    app_engine = create_async_engine(app_url)
    try:
        await asyncio.to_thread(command.upgrade, _alembic_config(), "head")
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            for tbl in ("tenants", "users", "collections"):
                await conn.execute(
                    text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {tbl} TO "{app_role}"')
                )

        tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
        coll_a, coll_b = uuid.uuid4(), uuid.uuid4()
        user_a, user_b = uuid.uuid4(), uuid.uuid4()
        owner_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        app_factory = async_sessionmaker(bind=app_engine, expire_on_commit=False)

        async with owner_factory() as sess:
            await bind_bypass(sess)
            for tid, uid, cid in ((tenant_a, user_a, coll_a), (tenant_b, user_b, coll_b)):
                await sess.execute(
                    text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
                    {"id": tid, "name": f"Tenant {tid}"},
                )
                await sess.execute(
                    text(
                        "INSERT INTO users (id, tenant_id, email, password_hash, roles) "
                        "VALUES (:id, :tid, :email, 'h', ARRAY['member'])"
                    ),
                    {"id": uid, "tid": tid, "email": f"u-{uid}@t.test"},
                )
                await sess.execute(
                    text(
                        "INSERT INTO collections (id, tenant_id, owner_id, name) "
                        "VALUES (:id, :tid, :oid, 'C')"
                    ),
                    {"id": cid, "tid": tid, "oid": uid},
                )
            await sess.commit()

        # --- four concurrent call scopes, interleaved tenants, one barrier ----
        started = 0
        all_started = asyncio.Event()

        async def _call_scope_reads(tenant_id: uuid.UUID) -> set[uuid.UUID]:
            nonlocal started
            # The exact per-call-scope shape the #412 executor uses: fresh
            # session → bind_tenant → (handler's) reads → close.
            async with app_factory() as sess:
                await bind_tenant(sess, tenant_id)
                started += 1
                if started >= 4:
                    all_started.set()
                # Hold every worker inside its bound transaction until ALL four
                # are bound — the overlap is real, not sequential.
                await asyncio.wait_for(all_started.wait(), timeout=10)
                rows = (
                    (await sess.execute(text("SELECT id, tenant_id FROM collections")))
                    .all()
                )
                assert all(r[1] == tenant_id for r in rows), "cross-tenant row leaked"
                return {r[0] for r in rows}

        results = await asyncio.gather(
            _call_scope_reads(tenant_a),
            _call_scope_reads(tenant_b),
            _call_scope_reads(tenant_a),
            _call_scope_reads(tenant_b),
        )
        assert results[0] == results[2] == {coll_a}
        assert results[1] == results[3] == {coll_b}
    finally:
        await engine.dispose()
        await app_engine.dispose()
        if orig is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = orig
        get_settings.cache_clear()
        admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{tmp_db}" WITH (FORCE)'))
                await conn.execute(text(f'DROP ROLE IF EXISTS "{app_role}"'))
        finally:
            await admin.dispose()


@_live
async def test_chat_runtime_call_scopes_keep_rls_isolation_on_postgres() -> None:
    """#412 / #433 round-2: the REAL runtime composition under live RLS.

    Drives an actual :class:`~app.services.chat_runtime.ChatRuntime` — not a
    hand-shaped session pattern — through a three-search fan-out turn on a
    migrated throwaway Postgres, connected as a NON-superuser, NON-bypassrls
    app role. The injected retrieval factory executes a REAL predicate-free
    ``SELECT`` on whatever session the runtime hands it, so this exercises the
    genuine ``_call_scope`` path: per-call sessions, ``bind_tenant`` on each,
    concurrent overlapping transactions (a start barrier guarantees it). RLS
    must confine every worker to the bound tenant, the answer must complete,
    and the factory must have seen one runtime session + three DISTINCT scope
    sessions.
    """
    import asyncio

    from alembic import command

    from app.auth.principal import Principal
    from app.domain.entities import Role as EntityRole
    from app.domain.llm import StreamEvent, ToolCall
    from app.domain.retrieval import RetrievedPassage
    from app.realtime.backplane import InMemoryBackplane
    from app.services.chat_runtime import ChatRuntime

    tmp_db = f"lumen_rls412c_{uuid.uuid4().hex[:12]}"
    app_role = f"lumen_rls412c_app_{uuid.uuid4().hex[:8]}"
    app_pw = "rls_test_pw"  # noqa: S105 — throwaway role in a throwaway DB
    admin_url = _swap_db(_PG_URL, "postgres")
    tmp_url = _swap_db(_PG_URL, tmp_db)
    parsed = urlparse(tmp_url)
    app_url = urlunparse(
        parsed._replace(netloc=f"{app_role}:{app_pw}@{parsed.hostname}:{parsed.port}")
    )

    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{tmp_db}"'))
            create_role = (
                f'CREATE ROLE "{app_role}" LOGIN ' f"PASSWORD '{app_pw}' NOSUPERUSER NOBYPASSRLS"
            )
            await conn.execute(text(create_role))
    finally:
        await admin.dispose()

    orig = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = tmp_url
    from app.core.config import get_settings

    get_settings.cache_clear()
    engine = create_async_engine(tmp_url)
    app_engine = create_async_engine(app_url)
    try:
        await asyncio.to_thread(command.upgrade, _alembic_config(), "head")
        grant_tables = (
            "tenants",
            "users",
            "chat_sessions",
            "messages",
            "citations",
            "collections",
            "documents",
            "chunks",
            "tool_invocations",
            "audit_events",
            "llm_usage",
        )
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            for tbl in grant_tables:
                await conn.execute(
                    text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {tbl} TO "{app_role}"')
                )

        tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
        user_a, user_b = uuid.uuid4(), uuid.uuid4()
        coll_a, coll_b = uuid.uuid4(), uuid.uuid4()
        doc_a, chunk_a = uuid.uuid4(), uuid.uuid4()
        chat_session_a = uuid.uuid4()
        owner_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        app_factory = async_sessionmaker(bind=app_engine, expire_on_commit=False)

        async with owner_factory() as sess:
            await bind_bypass(sess)
            for tid, uid, cid in ((tenant_a, user_a, coll_a), (tenant_b, user_b, coll_b)):
                await sess.execute(
                    text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
                    {"id": tid, "name": f"Tenant {tid}"},
                )
                await sess.execute(
                    text(
                        "INSERT INTO users (id, tenant_id, email, password_hash, roles) "
                        "VALUES (:id, :tid, :email, 'h', ARRAY['member'])"
                    ),
                    {"id": uid, "tid": tid, "email": f"u-{uid}@t.test"},
                )
                await sess.execute(
                    text(
                        "INSERT INTO collections (id, tenant_id, owner_id, name) "
                        "VALUES (:id, :tid, :oid, 'C')"
                    ),
                    {"id": cid, "tid": tid, "oid": uid},
                )
            await sess.execute(
                text(
                    "INSERT INTO documents (id, tenant_id, owner_id, collection_id, filename, "
                    "mime_type, size_bytes, storage_key, status) VALUES "
                    "(:id, :tid, :oid, :cid, 'taxes.pdf', 'application/pdf', 10, :key, 'ready')"
                ),
                {
                    "id": doc_a,
                    "tid": tenant_a,
                    "oid": user_a,
                    "cid": coll_a,
                    "key": f"{tenant_a}/t",
                },
            )
            await sess.execute(
                text(
                    "INSERT INTO chunks (id, tenant_id, document_id, ord, text, char_start, "
                    "char_end) VALUES (:id, :tid, :did, 0, 'The deduction is $14,600.', 0, 25)"
                ),
                {"id": chunk_a, "tid": tenant_a, "did": doc_a},
            )
            await sess.execute(
                text(
                    "INSERT INTO chat_sessions (id, tenant_id, owner_id, model, title) "
                    "VALUES (:id, :tid, :oid, 'test/model', 'rls-412')"
                ),
                {"id": chat_session_a, "tid": tenant_a, "oid": user_a},
            )
            await sess.commit()

        # --- the probe retrieval: REAL SQL on whatever session it is given ----
        started = 0
        all_started = asyncio.Event()
        seen_sessions: list[object] = []

        class _SqlProbeRetrieval:
            def __init__(self, session: AsyncSession) -> None:
                self._session = session

            async def search_text(
                self,
                *,
                principal: object,
                query: str,
                k: int,
                collection_ids: object = None,
                document_ids: object = None,
            ) -> list[RetrievedPassage]:
                nonlocal started
                started += 1
                if started >= 3:
                    all_started.set()
                # Hold until all three scopes are live — the overlap is real.
                await asyncio.wait_for(all_started.wait(), timeout=10)
                rows = (
                    await self._session.execute(text("SELECT id, tenant_id FROM collections"))
                ).all()
                # RLS confinement inside the concurrent call scope: only the
                # bound tenant's rows are visible, predicate-free.
                assert {r[1] for r in rows} == {tenant_a}, "cross-tenant row leaked in scope"
                assert {r[0] for r in rows} == {coll_a}
                return [
                    RetrievedPassage(
                        chunk_id=chunk_a,
                        document_id=doc_a,
                        document_name="taxes.pdf",
                        ord=0,
                        text="The deduction is $14,600.",
                        char_start=0,
                        char_end=25,
                        score=0.9,
                    )
                ]

            async def search_documents(
                self, *, principal: object, name_or_query: str, k: int = 10
            ) -> list[object]:
                return []

            async def get_document(
                self, *, principal: object, document_id: object
            ) -> object | None:
                return None

        def retrieval_factory(session: AsyncSession) -> _SqlProbeRetrieval:
            seen_sessions.append(session)
            return _SqlProbeRetrieval(session)

        class _ThreeSearchGateway:
            async def stream_tools(
                self,
                messages: object,
                *,
                tools: object,
                model: object = None,
                tool_choice: object = None,
                api_key: object = None,
                api_base: object = None,
                cache_key: object = None,
                        ):  # noqa: ANN202 — async generator
                msgs = list(messages)  # type: ignore[arg-type]
                has_tool = any(getattr(m.role, "value", "") == "tool" for m in msgs)
                if tool_choice == "none" or has_tool:
                    yield StreamEvent(text="Grounded answer.")
                    yield StreamEvent(finish_reason="stop")
                else:
                    yield StreamEvent(
                        tool_calls=(
                            ToolCall(id="c1", name="search_text", arguments={"query": "q1"}),
                            ToolCall(id="c2", name="search_text", arguments={"query": "q2"}),
                            ToolCall(id="c3", name="search_text", arguments={"query": "q3"}),
                        ),
                        finish_reason="tool_calls",
                    )

        principal = Principal(
            user_id=user_a, tenant_id=tenant_a, roles=(EntityRole.MEMBER,)
        )
        backplane = InMemoryBackplane()
        stream_id = uuid.uuid4().hex

        async def _drain() -> list[dict[str, object]]:
            return [env async for env in backplane.subscribe(stream_id)]

        consumer = asyncio.create_task(_drain())
        await asyncio.sleep(0)
        runtime = ChatRuntime(
            sessionmaker=app_factory,
            gateway=_ThreeSearchGateway(),  # type: ignore[arg-type]
            backplane=backplane,
            principal=principal,
            request_id="rls-412",
            source_ip="127.0.0.1",
            retrieval_factory=retrieval_factory,  # type: ignore[arg-type]
        )
        await runtime.run(
            stream_id=stream_id,
            session_id=chat_session_a,
            question="what is the deduction?",
            model="test/model",
            history=[],
            collection_ids=None,
        )
        envs = await asyncio.wait_for(consumer, timeout=10)

        assert envs[-1]["type"] == "done"
        assert started == 3  # all three scopes were live simultaneously
        results = [
            e
            for e in envs
            if e["type"] == "event" and e.get("name") == "tool_result"
        ]
        assert len(results) == 3
        assert all(e["data"]["ok"] is True for e in results)  # type: ignore[index]
        # The factory saw the runtime session + three DISTINCT call scopes.
        assert len(seen_sessions) == 4
        assert len({id(s) for s in seen_sessions}) == 4
    finally:
        await engine.dispose()
        await app_engine.dispose()
        if orig is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = orig
        get_settings.cache_clear()
        admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{tmp_db}" WITH (FORCE)'))
                await conn.execute(text(f'DROP ROLE IF EXISTS "{app_role}"'))
        finally:
            await admin.dispose()
