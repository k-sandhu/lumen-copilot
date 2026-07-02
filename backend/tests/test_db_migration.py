"""Migration tests for the MVP schema (issue #44, AC-1/AC-3).

Two layers, both offline-safe:

* **Offline DDL** (no DB) — render the ``0002`` upgrade *and* downgrade to SQL via
  Alembic's offline mode and assert the headline shapes are present: all nine
  MVP tables created, the ``vector(1024)`` embedding column, every ``tenant_id``
  FK, and the append-only ``REVOKE`` on ``audit_events``. The ``0003`` step
  (``refresh_tokens``, issue #19) gets its own round-trip check. This proves the
  migrations are *reversible* (AC-3: a downgrade exists and renders) offline.
* **Live apply/reverse** (compose Postgres) — actually run ``upgrade head`` then
  ``downgrade base`` against a real database and assert the schema appears and
  fully disappears. Skipped automatically when Postgres is unreachable, matching
  the offline-safe pattern in ``test_object_store.py``.

The model registry and the migration are kept in lockstep by an additional check
that every table on ``Base.metadata`` is created by the upgrade DDL.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.db.base import Base

# Importing models registers them on Base.metadata.
import app.db.models  # noqa: F401  isort: skip

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _BACKEND_ROOT / "alembic.ini"

# Tables created by the 0002 MVP-schema migration (the offline-DDL step checks
# render only that revision).
_MVP_TABLES = {
    "tenants",
    "users",
    "collections",
    "documents",
    "chunks",
    "chat_sessions",
    "messages",
    "citations",
    "audit_events",
}

# Every table the ORM registry now carries: the MVP set plus refresh_tokens
# (0003, issue #19, spec 0004 §2.3), sources (0006, issue #109, ADR-0009 §4),
# grants (0008, issue #18, spec 0004 §2.2 — explicit ACL grants), the spec-0005
# preferences/saved-search/recent tables (0009–0011, epic #144), and artifacts
# (0013, issue #208, CC-12 — agent/run-produced files).
_ALL_TABLES = _MVP_TABLES | {
    "refresh_tokens",
    "sources",
    "grants",
    "user_preferences",
    "saved_searches",
    "recent_searches",
    "artifacts",
}


def _alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    if url is not None:
        cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_metadata_covers_every_mvp_table() -> None:
    """The ORM registry and the spec-0004 table list agree."""
    assert set(Base.metadata.tables) == _ALL_TABLES


def test_migration_chain_is_linear_single_head() -> None:
    """The chain is linear 0001 → … → 0012 with a SINGLE head (ADR-0008 §4).

    The single-head invariant is the whole point of the one-migration-owner-per-wave
    rule: two new migrations would fork into two heads. ``get_heads()`` returning a
    one-element list is the offline form of the ``alembic heads`` == 1 acceptance.
    """
    script = ScriptDirectory.from_config(_alembic_config())
    assert list(script.get_heads()) == ["0013_artifacts"]
    mvp = script.get_revision("0002_mvp_schema")
    assert mvp is not None
    assert mvp.down_revision == "0001_enable_pgvector"
    rt = script.get_revision("0003_refresh_tokens")
    assert rt is not None
    assert rt.down_revision == "0002_mvp_schema"
    ri = script.get_revision("0004_retrieval_indexes")
    assert ri is not None
    assert ri.down_revision == "0003_refresh_tokens"
    aqi = script.get_revision("0005_audit_query_indexes")
    assert aqi is not None
    assert aqi.down_revision == "0004_retrieval_indexes"
    src = script.get_revision("0006_sources")
    assert src is not None
    assert src.down_revision == "0005_audit_query_indexes"
    rls = script.get_revision("0007_tenancy_rls")
    assert rls is not None
    assert rls.down_revision == "0006_sources"
    grants = script.get_revision("0008_grants")
    assert grants is not None
    assert grants.down_revision == "0007_tenancy_rls"
    prefs = script.get_revision("0009_user_preferences")
    assert prefs is not None
    assert prefs.down_revision == "0008_grants"
    saved = script.get_revision("0010_saved_searches")
    assert saved is not None
    assert saved.down_revision == "0009_user_preferences"
    recent = script.get_revision("0011_recent_searches")
    assert recent is not None
    assert recent.down_revision == "0010_saved_searches"
    tts = script.get_revision("0012_tenant_max_tool_turns")
    assert tts is not None
    assert tts.down_revision == "0011_recent_searches"
    art = script.get_revision("0013_artifacts")
    assert art is not None
    assert art.down_revision == "0012_tenant_max_tool_turns"


def test_offline_upgrade_sql_has_all_tables_and_vector_and_revoke(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-1: offline upgrade DDL creates every table + the vector(1024) column."""
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    # Render only the 0002 step as SQL (no DB connection in --sql mode).
    command.upgrade(cfg, "0001_enable_pgvector:0002_mvp_schema", sql=True)
    sql = capsys.readouterr().out.lower()

    for table in _MVP_TABLES:
        assert f"create table {table}" in sql, f"missing CREATE for {table}"
    # The pgvector embedding column, pinned to LLM_EMBEDDING_DIMENSIONS = 1024.
    assert "vector(1024)" in sql
    # Every tenant-scoped table references tenants(id) (INV-1 predicate column).
    assert "references tenants" in sql
    # audit_events is append-only: UPDATE/DELETE revoked.
    assert "revoke update, delete on table audit_events" in sql


def test_offline_downgrade_sql_drops_all_tables(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-3: the downgrade exists and renders, dropping every table."""
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.downgrade(cfg, "0002_mvp_schema:0001_enable_pgvector", sql=True)
    sql = capsys.readouterr().out.lower()

    for table in _MVP_TABLES:
        assert f"drop table {table}" in sql, f"missing DROP for {table}"
    # The append-only revoke is undone before the drop.
    assert "grant update, delete on table audit_events" in sql


def test_offline_refresh_tokens_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0003 creates refresh_tokens (FK → users) and the downgrade drops it."""
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0002_mvp_schema:0003_refresh_tokens", sql=True)
    up = capsys.readouterr().out.lower()
    assert "create table refresh_tokens" in up
    assert "references users" in up  # token rows hang off a user (CASCADE)
    assert "references tenants" in up  # tenant-scoped like every table (INV-1)

    command.downgrade(cfg, "0003_refresh_tokens:0002_mvp_schema", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop table refresh_tokens" in down


def test_offline_retrieval_indexes_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0004 creates the pgvector ANN + full-text GIN indexes; the downgrade drops them (#45)."""
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0003_refresh_tokens:0004_retrieval_indexes", sql=True)
    up = capsys.readouterr().out.lower()
    # pgvector HNSW ANN index over the cosine ops class (matches the query's <=>).
    assert "create index ix_chunks_embedding_hnsw" in up
    assert "using hnsw (embedding vector_cosine_ops)" in up
    # Full-text GIN index on the same expression the lexical query matches.
    assert "create index ix_chunks_text_fts" in up
    assert "using gin (to_tsvector('english', text))" in up

    command.downgrade(cfg, "0004_retrieval_indexes:0003_refresh_tokens", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop index if exists ix_chunks_text_fts" in down
    assert "drop index if exists ix_chunks_embedding_hnsw" in down


# The three composite (tenant_id, <filter>, ts) audit indexes 0005 adds — the
# names are stable and shared by the migration and the AuditEvent model so
# autogenerate stays clean.
_AUDIT_QUERY_INDEXES = (
    ("ix_audit_events_tenant_action_ts", "(tenant_id, action, ts)"),
    ("ix_audit_events_tenant_actor_ts", "(tenant_id, actor_id, ts)"),
    ("ix_audit_events_tenant_resource_ts", "(tenant_id, resource_id, ts)"),
)


def test_offline_audit_query_indexes_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0005 creates the three composite audit indexes; the downgrade drops them (#82)."""
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0004_retrieval_indexes:0005_audit_query_indexes", sql=True)
    up = capsys.readouterr().out.lower()
    for name, cols in _AUDIT_QUERY_INDEXES:
        # Composite index on audit_events, leading with tenant_id and ending in ts
        # so the equality filter and ORDER BY ts share one index.
        assert f"create index {name} on audit_events {cols}" in up, f"missing CREATE for {name}"

    command.downgrade(cfg, "0005_audit_query_indexes:0004_retrieval_indexes", sql=True)
    down = capsys.readouterr().out.lower()
    for name, _cols in _AUDIT_QUERY_INDEXES:
        assert f"drop index {name}" in down, f"missing DROP for {name}"


def test_offline_sources_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0006 creates ``sources`` + the ``documents.source_id`` link; down() reverses (#109).

    AC: the upgrade renders the tenant/owner-scoped ``sources`` table (with its
    JSONB ``config`` and the tenant-leading index), and the nullable
    ``source_id`` FK on ``documents`` with ``ON DELETE CASCADE``; the downgrade
    drops the column and the table. Offline DDL render (Postgres dialect) — the
    structural check that proves reversibility without a DB (#70 lesson).
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0005_audit_query_indexes:0006_sources", sql=True)
    up = capsys.readouterr().out.lower()
    # The tenant/owner-scoped sources table with its JSONB config column.
    assert "create table sources" in up
    assert "references tenants" in up  # tenant-scoped (INV-1)
    assert "references users" in up  # owner_id FK
    assert "config jsonb" in up
    assert "indexed_count" in up
    # Tenant-leading index (the INV-1 predicate column).
    assert "create index ix_sources_tenant_id on sources (tenant_id)" in up
    assert "create index ix_sources_owner_id on sources (owner_id)" in up
    # The nullable source_id FK on documents, CASCADE on source delete.
    assert "alter table documents add column source_id" in up
    assert "references sources" in up
    assert "on delete cascade" in up
    assert "create index ix_documents_source_id on documents (source_id)" in up

    command.downgrade(cfg, "0006_sources:0005_audit_query_indexes", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop index ix_documents_source_id" in down
    assert "alter table documents drop column source_id" in down
    assert "drop table sources" in down


# Every tenant-scoped table 0007 puts under RLS (the parent ``tenants`` is
# excluded — it has no ``tenant_id``). Kept in lockstep with the migration's
# ``_SCOPED_TABLES`` and ``app.db.models``' ``TenantScopedMixin`` users.
_RLS_TABLES = (
    "users",
    "refresh_tokens",
    "collections",
    "sources",
    "documents",
    "chunks",
    "chat_sessions",
    "messages",
    "citations",
    "audit_events",
)


def test_offline_tenancy_rls_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0007 enables+forces RLS with a per-table policy; the downgrade reverses (#17).

    AC (INV-1 backstop, spec 0004 §2.1): the upgrade renders, for **every**
    tenant-scoped table, ``ENABLE`` + ``FORCE ROW LEVEL SECURITY`` and a
    ``CREATE POLICY`` whose predicate is keyed off the ``app.tenant_id`` GUC with
    the fail-closed ``current_setting(..., true)`` form; ``tenants`` (the
    isolation root) is left alone. The downgrade drops every policy and disables
    RLS. Offline DDL render (Postgres dialect) — structural reversibility without
    a DB (#70 lesson); the behavioural proof is the live RLS negative tests.
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0006_sources:0007_tenancy_rls", sql=True)
    up = capsys.readouterr().out.lower()

    for table in _RLS_TABLES:
        assert f"alter table {table} enable row level security" in up, f"no ENABLE for {table}"
        assert f"alter table {table} force row level security" in up, f"no FORCE for {table}"
        assert f"create policy rls_{table} on {table}" in up, f"no policy for {table}"
    # The fail-closed GUC predicate + the explicit bypass sentinel (the only
    # tenant-agnostic exemption). ``current_setting(..., true)`` → NULL when unset.
    assert "current_setting('app.tenant_id', true)" in up
    assert "= 'bypass'" in up
    assert "with check" in up
    # The parent tenants table is NOT put under RLS (it carries no tenant_id).
    assert "alter table tenants enable row level security" not in up

    command.downgrade(cfg, "0007_tenancy_rls:0006_sources", sql=True)
    down = capsys.readouterr().out.lower()
    for table in _RLS_TABLES:
        assert f"drop policy if exists rls_{table} on {table}" in down, f"no DROP POLICY {table}"
        assert f"alter table {table} disable row level security" in down, f"no DISABLE {table}"


def test_offline_grants_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0008 creates the tenant/owner-scoped ``grants`` table + RLS; down() reverses (#18).

    AC (spec 0004 §2.2, CC-1): the upgrade renders the ``grants`` table with its
    ``resource_type``/``resource_id`` + ``principal_type``/``principal_id`` columns,
    the UNIQUE on the (resource, principal) tuple, the two composite indexes (by
    principal for the retrieval filter, by resource for the grant service), and the
    same fail-closed RLS policy the 0007 backstop uses (``grants`` is tenant-scoped,
    INV-1). The downgrade drops the policy, disables RLS, and drops the table.
    Offline DDL render (Postgres dialect) — structural reversibility without a DB
    (#70 lesson); the behavioural proof is the live grant retrieval tests.
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0007_tenancy_rls:0008_grants", sql=True)
    up = capsys.readouterr().out.lower()
    # The tenant/owner-scoped grants table + its FKs.
    assert "create table grants" in up
    assert "references tenants" in up  # tenant-scoped (INV-1)
    assert "references users" in up  # granted_by FK
    assert "resource_type" in up
    assert "principal_type" in up
    # The idempotency UNIQUE on (tenant, resource, principal).
    assert "uq_grants_resource_principal" in up
    # The two composite indexes (retrieval filter by principal; service by resource).
    assert "create index ix_grants_tenant_principal on grants (tenant_id, principal_id)" in up
    assert (
        "create index ix_grants_tenant_resource on grants "
        "(tenant_id, resource_type, resource_id)" in up
    )
    # The RLS backstop (grants is tenant-scoped) — same fail-closed GUC policy as 0007.
    assert "alter table grants enable row level security" in up
    assert "alter table grants force row level security" in up
    assert "create policy rls_grants on grants" in up
    assert "current_setting('app.tenant_id', true)" in up

    command.downgrade(cfg, "0008_grants:0007_tenancy_rls", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop policy if exists rls_grants on grants" in down
    assert "alter table grants disable row level security" in down
    assert "drop index ix_grants_tenant_resource" in down
    assert "drop index ix_grants_tenant_principal" in down
    assert "drop table grants" in down


def test_offline_tenant_max_tool_turns_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0012 adds ``tenants.max_tool_turns`` (+ its range check); down() reverses (#148).

    AC: the upgrade renders the nullable ``max_tool_turns`` column on ``tenants``
    and the ``ck_tenants_max_tool_turns_range`` 1–50 check; the downgrade drops the
    constraint and the column. Offline DDL render (Postgres dialect) — structural
    reversibility without a DB (#70 lesson).
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0011_recent_searches:0012_tenant_max_tool_turns", sql=True)
    up = capsys.readouterr().out.lower()
    # The per-tenant override column + its bounded check (issue #148).
    assert "alter table tenants add column max_tool_turns" in up
    assert "ck_tenants_max_tool_turns_range" in up

    command.downgrade(cfg, "0012_tenant_max_tool_turns:0011_recent_searches", sql=True)
    down = capsys.readouterr().out.lower()
    assert "alter table tenants drop column max_tool_turns" in down


def test_offline_artifacts_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0013 creates the tenant/owner-scoped ``artifacts`` table + RLS; down() reverses (#208).

    AC (CC-12, spec 0004 §2.1/§2.2): the upgrade renders the ``artifacts`` table
    with its ``produced_by`` + storage-key/sha256 columns, the nullable link ids
    (only ``session_id`` an FK), the size + ``produced_by`` CHECKs, the tenant/owner
    + session + retention indexes, and the same fail-closed RLS policy the 0007
    backstop uses (``artifacts`` is tenant-scoped, INV-1). The downgrade drops the
    policy, disables RLS, drops the indexes, and drops the table. Offline DDL render
    (Postgres dialect) — structural reversibility without a DB (#70 lesson).
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0012_tenant_max_tool_turns:0013_artifacts", sql=True)
    up = capsys.readouterr().out.lower()
    # The tenant/owner-scoped artifacts table + its FKs.
    assert "create table artifacts" in up
    assert "references tenants" in up  # tenant-scoped (INV-1)
    assert "references users" in up  # owner_id FK
    assert "references chat_sessions" in up  # the only real link FK (session_id)
    assert "produced_by" in up
    assert "storage_key" in up
    assert "sha256" in up
    assert "retention_expires_at" in up
    # The domain-pinning CHECKs.
    assert "ck_artifacts_produced_by" in up
    assert "ck_artifacts_size_nonneg" in up
    # The list + retention indexes.
    assert "create index ix_artifacts_tenant_owner on artifacts (tenant_id, owner_id)" in up
    assert "create index ix_artifacts_session_id on artifacts (session_id)" in up
    # The retention sweep index is partial on Postgres (keep-forever rows excluded).
    assert "create index ix_artifacts_retention on artifacts (retention_expires_at)" in up
    assert "where retention_expires_at is not null" in up
    # The RLS backstop — same fail-closed GUC policy as 0007.
    assert "alter table artifacts enable row level security" in up
    assert "alter table artifacts force row level security" in up
    assert "create policy rls_artifacts on artifacts" in up
    assert "current_setting('app.tenant_id', true)" in up

    command.downgrade(cfg, "0013_artifacts:0012_tenant_max_tool_turns", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop policy if exists rls_artifacts on artifacts" in down
    assert "alter table artifacts disable row level security" in down
    assert "drop index ix_artifacts_retention" in down
    assert "drop index ix_artifacts_session_id" in down
    assert "drop index ix_artifacts_tenant_owner" in down
    assert "drop table artifacts" in down


def test_audit_index_names_match_model_and_migration() -> None:
    """The migration's index names are exactly those declared on the ORM model.

    Keeps the migration and ``AuditEvent.__table_args__`` in lockstep (so a live
    ``alembic check`` / autogenerate sees no drift): the model is the source of the
    expected names, the migration must create that same set, nothing more.
    """
    from sqlalchemy import Index

    model_indexes = {
        ix.name for ix in Base.metadata.tables["audit_events"].indexes if isinstance(ix, Index)
    }
    for name, _cols in _AUDIT_QUERY_INDEXES:
        assert name in model_indexes, f"{name} created by 0005 but not declared on the model"


# ---------------------------------------------------------------------------
# Live apply/reverse against compose Postgres (AC-1/AC-3). Skipped if offline.
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
    """Return ``url`` with its database path replaced by ``dbname``."""
    return urlunparse(urlparse(url)._replace(path=f"/{dbname}"))


_live = pytest.mark.skipif(
    not _pg_reachable(_PG_URL),
    reason=f"Postgres not reachable for {_PG_URL}; live migration test skipped (offline-safe).",
)


@_live
async def test_live_upgrade_then_downgrade_round_trip() -> None:
    """AC-1 + AC-3: apply head, assert tables exist, then fully reverse.

    Runs the project's real async migration path (env.py → asyncpg) against a
    **disposable throwaway database** — NEVER the app/CI database. A migration
    round-trip ends in ``downgrade base`` (every table dropped), so pointing it
    at a populated DB would destroy data; we CREATE a temp database, run
    upgrade→inspect→downgrade→inspect there, and DROP it in teardown. The
    ``alembic`` commands run in a worker thread because env.py spins up its own
    event loop (``asyncio.run``); env.py reads the URL from Settings, so we
    redirect the run via ``DATABASE_URL`` + a settings-cache reset.
    """
    import asyncio
    import uuid

    from alembic import command
    from sqlalchemy import inspect, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import get_settings

    tmp_db = f"lumen_migtest_{uuid.uuid4().hex[:12]}"
    admin_url = _swap_db(_PG_URL, "postgres")  # maintenance DB for CREATE/DROP
    tmp_url = _swap_db(_PG_URL, tmp_db)

    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{tmp_db}"'))
    finally:
        await admin.dispose()

    orig = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = tmp_url
    get_settings.cache_clear()
    engine = create_async_engine(tmp_url)
    try:
        await asyncio.to_thread(command.upgrade, _alembic_config(), "head")
        async with engine.connect() as conn:
            names = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
        assert _ALL_TABLES <= names

        await asyncio.to_thread(command.downgrade, _alembic_config(), "base")
        async with engine.connect() as conn:
            names_after = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
        # Every MVP table is gone (alembic_version may remain; that's fine).
        assert not (_ALL_TABLES & names_after)
    finally:
        await engine.dispose()
        if orig is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = orig
        get_settings.cache_clear()
        admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{tmp_db}" WITH (FORCE)'))
        finally:
            await admin.dispose()
