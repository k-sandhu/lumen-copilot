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
# preferences/saved-search/recent tables (0009–0011, epic #144), and
# tool_invocations (0013, issue #207 — the governed tool platform trace).
_ALL_TABLES = _MVP_TABLES | {
    "refresh_tokens",
    "sources",
    "grants",
    "user_preferences",
    "saved_searches",
    "recent_searches",
    "tool_invocations",
    "artifacts",
    "secrets",
    "assistants",
    "assistant_versions",
    "runs",
    "run_steps",
    "schedules",
    "code_runs",
    "mcp_servers",
    "tenant_tool_policy",
    "tenant_sandbox_policy",
    "tenant_autonomy_policy",
    "llm_providers",
    "run_deliveries",
    # 0032, issue #409 — per-answer token & cache usage accounting.
    "llm_usage",
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


def test_every_revision_id_fits_alembic_version_column() -> None:
    """Every revision id must fit ``alembic_version.version_num`` — varchar(32).

    The #402 lesson: a 39-char revision id passes every offline test (offline
    DDL never touches ``alembic_version``), then crash-loops the backend at
    boot when the live version UPDATE truncates. Pin the constraint here so an
    oversized id fails in CI, not in a deploy.
    """
    script = ScriptDirectory.from_config(_alembic_config())
    for rev in script.walk_revisions():
        assert len(rev.revision) <= 32, (
            f"revision id {rev.revision!r} is {len(rev.revision)} chars — "
            "alembic_version.version_num is varchar(32); shorten the id."
        )


def test_migration_chain_is_linear_single_head() -> None:
    """The chain is linear 0001 → … → 0013 with a SINGLE head (ADR-0008 §4).

    The single-head invariant is the whole point of the one-migration-owner-per-wave
    rule: two new migrations would fork into two heads. ``get_heads()`` returning a
    one-element list is the offline form of the ``alembic heads`` == 1 acceptance.
    """
    script = ScriptDirectory.from_config(_alembic_config())
    assert list(script.get_heads()) == ["0035_tenant_fallbacks"]
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
    tools = script.get_revision("0013_tool_invocations")
    assert tools is not None
    assert tools.down_revision == "0012_tenant_max_tool_turns"
    art = script.get_revision("0014_artifacts")
    assert art is not None
    assert art.down_revision == "0013_tool_invocations"
    secrets = script.get_revision("0015_secrets")
    assert secrets is not None
    assert secrets.down_revision == "0014_artifacts"
    assistants = script.get_revision("0016_assistants")
    assert assistants is not None
    assert assistants.down_revision == "0015_secrets"
    runs = script.get_revision("0017_runs")
    assert runs is not None
    assert runs.down_revision == "0016_assistants"
    schedules = script.get_revision("0018_schedules")
    assert schedules is not None
    assert schedules.down_revision == "0017_runs"
    code_runs = script.get_revision("0019_code_runs")
    assert code_runs is not None
    assert code_runs.down_revision == "0018_schedules"
    mcp_servers = script.get_revision("0020_mcp_servers")
    assert mcp_servers is not None
    assert mcp_servers.down_revision == "0019_code_runs"
    tool_policy = script.get_revision("0021_tenant_tool_policy")
    assert tool_policy is not None
    assert tool_policy.down_revision == "0020_mcp_servers"
    sandbox_policy = script.get_revision("0022_sandbox_policy")
    assert sandbox_policy is not None
    assert sandbox_policy.down_revision == "0021_tenant_tool_policy"
    assistant_governance = script.get_revision("0023_assistant_governance")
    assert assistant_governance is not None
    assert assistant_governance.down_revision == "0022_sandbox_policy"
    autonomy_policy = script.get_revision("0024_autonomy_policy")
    assert autonomy_policy is not None
    assert autonomy_policy.down_revision == "0023_assistant_governance"
    run_deliveries = script.get_revision("0025_run_deliveries")
    assert run_deliveries is not None
    assert run_deliveries.down_revision == "0024_autonomy_policy"
    toolinv_fk = script.get_revision("0026_toolinv_msg_fk_deferrable")
    assert toolinv_fk is not None
    assert toolinv_fk.down_revision == "0025_run_deliveries"
    tenant_logo = script.get_revision("0027_tenant_logo")
    assert tenant_logo is not None
    assert tenant_logo.down_revision == "0026_toolinv_msg_fk_deferrable"
    llm_providers = script.get_revision("0028_llm_providers")
    assert llm_providers is not None
    assert llm_providers.down_revision == "0027_tenant_logo"
    user_settings = script.get_revision("0029_user_settings")
    assert user_settings is not None
    assert user_settings.down_revision == "0028_llm_providers"
    toolinv_summary = script.get_revision("0030_toolinv_result_summary")
    assert toolinv_summary is not None
    assert toolinv_summary.down_revision == "0029_user_settings"
    toolinv_ordinal = script.get_revision("0031_toolinv_ordinal_msg_idx")
    assert toolinv_ordinal is not None
    assert toolinv_ordinal.down_revision == "0030_toolinv_result_summary"
    llm_usage = script.get_revision("0032_llm_usage")
    assert llm_usage is not None
    assert llm_usage.down_revision == "0031_toolinv_ordinal_msg_idx"


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


def test_offline_tool_invocations_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0013 creates the ``tool_invocations`` trace table + RLS; down() reverses (#207).

    AC (issue #207 §4): the upgrade renders the tenant-scoped ``tool_invocations``
    table with its ``session_id``/``message_id`` FKs (SET NULL), the ``args_hash``
    + ``ok``/``error`` + ``duration_ms`` columns, the non-negative-duration check,
    the two tenant-leading indexes (by session for the trace, by tool for
    analytics), and the same fail-closed RLS policy the 0007 backstop uses
    (tenant-scoped, INV-1). The downgrade drops the policy, disables RLS, and drops
    the table. Offline DDL render (Postgres dialect) — structural reversibility
    without a DB (#70 lesson); the behavioural proof is the runner tests.
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0012_tenant_max_tool_turns:0013_tool_invocations", sql=True)
    up = capsys.readouterr().out.lower()
    assert "create table tool_invocations" in up
    assert "references tenants" in up  # tenant-scoped (INV-1)
    assert "references chat_sessions" in up  # session_id FK
    assert "references messages" in up  # message_id FK
    assert "args_hash" in up
    assert "duration_ms" in up
    assert "ck_tool_invocations_duration_nonneg" in up
    assert (
        "create index ix_tool_invocations_tenant_session on tool_invocations "
        "(tenant_id, session_id)" in up
    )
    assert (
        "create index ix_tool_invocations_tenant_tool on tool_invocations "
        "(tenant_id, tool_name)" in up
    )
    # The RLS backstop — same fail-closed GUC policy as 0007.
    assert "alter table tool_invocations enable row level security" in up
    assert "alter table tool_invocations force row level security" in up
    assert "create policy rls_tool_invocations on tool_invocations" in up
    assert "current_setting('app.tenant_id', true)" in up

    command.downgrade(cfg, "0013_tool_invocations:0012_tenant_max_tool_turns", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop policy if exists rls_tool_invocations on tool_invocations" in down
    assert "alter table tool_invocations disable row level security" in down
    assert "drop index ix_tool_invocations_tenant_tool" in down
    assert "drop index ix_tool_invocations_tenant_session" in down
    assert "drop table tool_invocations" in down



def test_offline_artifacts_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0014 creates the tenant/owner-scoped ``artifacts`` table + RLS; down() reverses (#208).

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
    command.upgrade(cfg, "0013_tool_invocations:0014_artifacts", sql=True)
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

    command.downgrade(cfg, "0014_artifacts:0013_tool_invocations", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop policy if exists rls_artifacts on artifacts" in down
    assert "alter table artifacts disable row level security" in down
    assert "drop index ix_artifacts_retention" in down
    assert "drop index ix_artifacts_session_id" in down
    assert "drop index ix_artifacts_tenant_owner" in down
    assert "drop table artifacts" in down


def test_offline_secrets_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0015 creates the tenant/owner-scoped ``secrets`` table + RLS; down() reverses (#209).

    AC (issue #209 §1): the upgrade renders the ``secrets`` table with its
    ``bytea`` ``ciphertext``/``nonce`` envelope columns, ``key_version``, the
    ``kind`` check, the per-owner-name UNIQUE, the tenant/owner index, and the same
    fail-closed RLS policy the 0007 backstop uses (``secrets`` is tenant-scoped,
    INV-1). The downgrade drops the policy, disables RLS, and drops the table.
    Offline DDL render (Postgres dialect) — structural reversibility without a DB
    (#70 lesson); the behavioural proof is the crypto + service tests.
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0014_artifacts:0015_secrets", sql=True)
    up = capsys.readouterr().out.lower()
    # The tenant/owner-scoped secrets table + its FKs.
    assert "create table secrets" in up
    assert "references tenants" in up  # tenant-scoped (INV-1)
    assert "references users" in up  # owner_id + created_by FKs
    # The envelope columns are bytea — never a plaintext column (#209).
    assert "ciphertext bytea" in up
    assert "nonce bytea" in up
    assert "key_version" in up
    # The kind enum domain is pinned at the DB; key_version must be >= 1.
    assert "ck_secrets_kind" in up
    assert "ck_secrets_key_version_positive" in up
    # A secret name is a per-owner singleton (re-store rotates in place).
    assert "uq_secrets_owner_name" in up
    assert "create index ix_secrets_tenant_owner on secrets (tenant_id, owner_id)" in up
    # The RLS backstop (secrets is tenant-scoped) — same fail-closed GUC policy as 0007.
    assert "alter table secrets enable row level security" in up
    assert "alter table secrets force row level security" in up
    assert "create policy rls_secrets on secrets" in up
    assert "current_setting('app.tenant_id', true)" in up

    command.downgrade(cfg, "0015_secrets:0014_artifacts", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop policy if exists rls_secrets on secrets" in down
    assert "alter table secrets disable row level security" in down
    assert "drop index ix_secrets_tenant_owner" in down
    assert "drop table secrets" in down


def test_offline_assistants_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0016 creates assistants + immutable assistant_versions + chat FKs; down() reverses (#211).

    AC (ADR-0011 §1): the upgrade renders the tenant/owner-scoped ``assistants``
    head (with its jsonb ``knowledge_scope``/``tool_allowlist`` and the autonomy /
    status CHECKs), the immutable ``assistant_versions`` table (UNIQUE per
    assistant, the version-positive CHECK, and the append-only UPDATE/DELETE
    REVOKE), the two nullable pin FKs on ``chat_sessions`` (SET NULL), and the same
    fail-closed RLS policy the 0007 backstop uses on both new tables. The downgrade
    re-grants the version write perms, drops the policies, drops the chat FK
    columns, and drops the tables. Offline DDL render (Postgres dialect) —
    structural reversibility without a DB (#70 lesson); the behavioural proof is
    the service + repository tests.
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0015_secrets:0016_assistants", sql=True)
    up = capsys.readouterr().out.lower()
    # The tenant/owner-scoped assistants head + its FKs.
    assert "create table assistants" in up
    assert "create table assistant_versions" in up
    assert "references tenants" in up  # tenant-scoped (INV-1)
    assert "references users" in up  # owner_id / backup_owner_id / author_id FKs
    assert "references assistants" in up  # version.assistant_id + chat pin FK
    # The jsonb config columns on the head.
    assert "knowledge_scope jsonb" in up
    assert "tool_allowlist jsonb" in up
    # The domain-pinning CHECKs.
    assert "ck_assistants_autonomy_level" in up
    assert "ck_assistants_status" in up
    assert "ck_assistant_versions_version_positive" in up
    # The per-assistant monotonic-version UNIQUE.
    assert "uq_assistant_versions_assistant_version" in up
    # The version history is append-only: the app role loses UPDATE/DELETE on it.
    assert "revoke update, delete on table assistant_versions" in up
    # The two nullable pin FKs on chat_sessions (additive; SET NULL).
    assert "alter table chat_sessions add column assistant_id" in up
    assert "alter table chat_sessions add column assistant_version_id" in up
    assert "on delete set null" in up
    # The RLS backstop on both new tables — same fail-closed GUC policy as 0007.
    assert "alter table assistants enable row level security" in up
    assert "alter table assistants force row level security" in up
    assert "create policy rls_assistants on assistants" in up
    assert "alter table assistant_versions enable row level security" in up
    assert "create policy rls_assistant_versions on assistant_versions" in up
    assert "current_setting('app.tenant_id', true)" in up

    command.downgrade(cfg, "0016_assistants:0015_secrets", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop policy if exists rls_assistants on assistants" in down
    assert "drop policy if exists rls_assistant_versions on assistant_versions" in down
    # The append-only revoke is undone before the drop.
    assert "grant update, delete on table assistant_versions" in down
    assert "alter table chat_sessions drop column assistant_version_id" in down
    assert "alter table chat_sessions drop column assistant_id" in down
    assert "drop table assistant_versions" in down
    assert "drop table assistants" in down


def test_offline_runs_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0017 creates runs + run_steps + RLS; down() reverses (#235, ADR-0015 §2).

    AC (ADR-0015 §2): the upgrade renders the tenant/owner-scoped ``runs`` table
    (with its jsonb ``inputs``/``error``, the trigger + status CHECKs, the pinned
    ``assistant_version_id`` + ``session_id`` + ``message_id`` SET-NULL FKs, and the
    four tenant-leading filter indexes), the ``run_steps`` transcript (the per-run
    ``(run_id, seq)`` UNIQUE, the seq-nonneg + kind CHECKs), and the same fail-closed
    RLS policy the 0007 backstop uses on both new tables. The downgrade drops the
    policies, disables RLS, and drops the tables (children → parents). Offline DDL
    render (Postgres dialect) — structural reversibility without a DB (#70 lesson);
    the behavioural proof is the runtime + service tests.
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0016_assistants:0017_runs", sql=True)
    up = capsys.readouterr().out.lower()
    # The tenant/owner-scoped runs table + its FKs.
    assert "create table runs" in up
    assert "create table run_steps" in up
    assert "references tenants" in up  # tenant-scoped (INV-1)
    assert "references users" in up  # owner_id FK (the run's principal)
    assert "references assistants" in up  # assistant_id FK
    assert "references assistant_versions" in up  # the pinned version (SET NULL)
    assert "references chat_sessions" in up  # the internal session (SET NULL)
    assert "references messages" in up  # the produced message (SET NULL)
    assert "references runs" in up  # run_steps.run_id FK (CASCADE)
    # The jsonb columns.
    assert "inputs jsonb" in up
    assert "payload jsonb" in up
    # The domain-pinning CHECKs.
    assert "ck_runs_trigger" in up
    assert "ck_runs_status" in up
    assert "ck_run_steps_seq_nonneg" in up
    assert "ck_run_steps_kind" in up
    # The per-run monotonic-seq UNIQUE (the transcript ordering guarantee).
    assert "uq_run_steps_run_seq" in up
    # The four ``/runs`` filter indexes (owner inbox, by assistant, schedule, status).
    assert "create index ix_runs_tenant_owner on runs (tenant_id, owner_id)" in up
    assert "create index ix_runs_tenant_assistant on runs (tenant_id, assistant_id)" in up
    assert "create index ix_runs_tenant_schedule on runs (tenant_id, schedule_id)" in up
    assert "create index ix_runs_tenant_status on runs (tenant_id, status)" in up
    # The RLS backstop on both new tables — same fail-closed GUC policy as 0007.
    assert "alter table runs enable row level security" in up
    assert "alter table runs force row level security" in up
    assert "create policy rls_runs on runs" in up
    assert "alter table run_steps enable row level security" in up
    assert "create policy rls_run_steps on run_steps" in up
    assert "current_setting('app.tenant_id', true)" in up

    command.downgrade(cfg, "0017_runs:0016_assistants", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop policy if exists rls_runs on runs" in down
    assert "drop policy if exists rls_run_steps on run_steps" in down
    assert "drop table run_steps" in down
    assert "drop table runs" in down


def test_offline_schedules_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0018 creates schedules + the runs.schedule_id FK + RLS; down() reverses (#236).

    AC (ADR-0015 §2): the upgrade renders the tenant/owner-scoped ``schedules`` table
    (with its jsonb ``input_params``/``delivery``/``cadence_structured``, the
    normalized ``cadence_cron``, the ``timezone``, the overlap-policy + last-status
    CHECKs, the ``assistant_id`` CASCADE FK + the ``owner_id`` SET-NULL FK, and the
    three tenant-leading indexes), the ``runs.schedule_id`` → ``schedules.id``
    ``ON DELETE SET NULL`` FK (the #235 residual), and the same fail-closed RLS policy
    the 0007 backstop uses. The downgrade drops the FK, the policy, disables RLS, and
    drops the table. Offline DDL render (Postgres dialect) — structural reversibility
    without a DB (#70 lesson); the behavioural proof is the service + scheduler tests.
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0017_runs:0018_schedules", sql=True)
    up = capsys.readouterr().out.lower()
    # The tenant/owner-scoped schedules table + its FKs.
    assert "create table schedules" in up
    assert "references tenants" in up  # tenant-scoped (INV-1)
    assert "references users" in up  # owner_id FK (the run's principal, SET NULL)
    assert "references assistants" in up  # assistant_id FK (CASCADE)
    # The cadence + jsonb columns.
    assert "cadence_cron" in up
    assert "input_params jsonb" in up
    assert "delivery jsonb" in up
    assert "timezone" in up
    # The domain-pinning CHECKs.
    assert "ck_schedules_overlap_policy" in up
    assert "ck_schedules_last_status" in up
    # The three tenant-leading indexes (owner list, by assistant, enabled sweep).
    assert "create index ix_schedules_tenant_owner on schedules (tenant_id, owner_id)" in up
    assert (
        "create index ix_schedules_tenant_assistant on schedules "
        "(tenant_id, assistant_id)" in up
    )
    assert "create index ix_schedules_tenant_enabled on schedules (tenant_id, enabled)" in up
    # The #235 residual FK: runs.schedule_id → schedules.id, SET NULL.
    assert "alter table runs add constraint fk_runs_schedule_id" in up
    assert "foreign key(schedule_id) references schedules (id) on delete set null" in up
    # The RLS backstop — same fail-closed GUC policy as 0007.
    assert "alter table schedules enable row level security" in up
    assert "alter table schedules force row level security" in up
    assert "create policy rls_schedules on schedules" in up
    assert "current_setting('app.tenant_id', true)" in up

    command.downgrade(cfg, "0018_schedules:0017_runs", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop policy if exists rls_schedules on schedules" in down
    assert "alter table schedules disable row level security" in down
    # The residual FK is dropped before the table.
    assert "alter table runs drop constraint fk_runs_schedule_id" in down
    assert "drop index ix_schedules_tenant_enabled" in down
    assert "drop table schedules" in down


def test_offline_code_runs_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0019 creates the tenant/owner-scoped ``code_runs`` table + RLS; down() reverses (#230).

    AC (ADR-0013 §4, spec 0004 §2.1/§2.2): the upgrade renders the ``code_runs`` table
    with its ``owner_id`` (SET NULL) + ``session_id`` (SET NULL) FKs, the status +
    exit-code/duration CHECKs, the ``code``/``stdout``/``stderr``/``image_digest``
    columns, the ``resource_usage`` jsonb, the ``artifact_ids`` array, the three
    tenant-leading indexes (owner history, status filter, session link), and the same
    fail-closed RLS policy the 0007 backstop uses (``code_runs`` is tenant-scoped,
    INV-1). The downgrade drops the policy, disables RLS, drops the indexes, and drops
    the table. Offline DDL render (Postgres dialect) — structural reversibility without
    a DB (#70 lesson); the behavioural proof is the sandbox service + isolation tests.
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0018_schedules:0019_code_runs", sql=True)
    up = capsys.readouterr().out.lower()
    # The tenant/owner-scoped code_runs table + its FKs.
    assert "create table code_runs" in up
    assert "references tenants" in up  # tenant-scoped (INV-1)
    assert "references users" in up  # owner_id FK (the run's principal, SET NULL)
    assert "references chat_sessions" in up  # session_id FK (SET NULL)
    # The captured-result + reproducibility columns.
    assert "code text" in up
    assert "stdout text" in up
    assert "stderr text" in up
    assert "image_digest" in up
    assert "resource_usage jsonb" in up
    assert "artifact_ids" in up
    # The domain-pinning CHECKs.
    assert "ck_code_runs_status" in up
    assert "ck_code_runs_exit_code_nonneg" in up
    assert "ck_code_runs_duration_nonneg" in up
    # The three tenant-leading indexes (owner history, status filter, session link).
    assert "create index ix_code_runs_tenant_owner on code_runs (tenant_id, owner_id)" in up
    assert "create index ix_code_runs_tenant_status on code_runs (tenant_id, status)" in up
    assert "create index ix_code_runs_session_id on code_runs (session_id)" in up
    # The RLS backstop — same fail-closed GUC policy as 0007.
    assert "alter table code_runs enable row level security" in up
    assert "alter table code_runs force row level security" in up
    assert "create policy rls_code_runs on code_runs" in up
    assert "current_setting('app.tenant_id', true)" in up

    command.downgrade(cfg, "0019_code_runs:0018_schedules", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop policy if exists rls_code_runs on code_runs" in down
    assert "alter table code_runs disable row level security" in down
    assert "drop index ix_code_runs_session_id" in down
    assert "drop index ix_code_runs_tenant_status" in down
    assert "drop index ix_code_runs_tenant_owner" in down
    assert "drop table code_runs" in down


def test_offline_mcp_servers_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0020 creates the tenant/owner-scoped ``mcp_servers`` table + RLS; down() reverses (#226).

    AC (ADR-0012 §5, spec 0004 §2.1/§2.2): the upgrade renders the ``mcp_servers``
    table with its ``owner_id`` (CASCADE) FK, the ``auth_secret_ref`` FK → ``secrets``
    (SET NULL — the credential lives in the CC-C vault, never this row), the
    transport + status CHECKs (remote transports only — ``stdio`` can never be
    stored), the ``endpoint_url`` / ``secret_hint`` / ``last_error`` columns, the
    ``discovered_tools`` jsonb, the tenant-leading indexes, and the same fail-closed
    RLS policy the 0007 backstop uses (``mcp_servers`` is tenant-scoped, INV-1). The
    downgrade drops the policy, disables RLS, drops the indexes, and drops the table.
    Offline DDL render (Postgres dialect) — structural reversibility without a DB
    (#70 lesson); the behavioural proof is the mcp_servers API/service tests.
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0019_code_runs:0020_mcp_servers", sql=True)
    up = capsys.readouterr().out.lower()
    # The tenant/owner-scoped mcp_servers table + its FKs.
    assert "create table mcp_servers" in up
    assert "references tenants" in up  # tenant-scoped (INV-1)
    assert "references users" in up  # owner_id FK (the registering user, CASCADE)
    assert "references secrets" in up  # auth_secret_ref FK (→ CC-C vault, SET NULL)
    # The endpoint + secret-hint + health columns (never the credential value).
    assert "endpoint_url text" in up
    assert "secret_hint" in up
    assert "last_error text" in up
    assert "discovered_tools jsonb" in up
    # The domain-pinning CHECKs (remote transports only; the health state machine).
    assert "ck_mcp_servers_transport" in up
    assert "ck_mcp_servers_status" in up
    # The tenant-leading index (owner list/lookup path).
    assert "create index ix_mcp_servers_tenant_owner on mcp_servers (tenant_id, owner_id)" in up
    # The RLS backstop — same fail-closed GUC policy as 0007.
    assert "alter table mcp_servers enable row level security" in up
    assert "alter table mcp_servers force row level security" in up
    assert "create policy rls_mcp_servers on mcp_servers" in up
    assert "current_setting('app.tenant_id', true)" in up

    command.downgrade(cfg, "0020_mcp_servers:0019_code_runs", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop policy if exists rls_mcp_servers on mcp_servers" in down
    assert "alter table mcp_servers disable row level security" in down
    assert "drop index ix_mcp_servers_tenant_owner" in down
    assert "drop table mcp_servers" in down


def test_offline_tenant_tool_policy_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0021 creates the tenant-scoped ``tenant_tool_policy`` table + RLS; down() reverses (#223).

    AC (issue #223, spec 0004 §2.1/§2.5): the upgrade renders the ``tenant_tool_policy``
    table with its ``tool_name`` + ``enabled`` / ``requires_approval`` columns, the
    ``updated_by`` FK → ``users`` (SET NULL — a deprovisioned admin does not
    cascade-delete the tenant's live policy), the per-tenant-per-tool UNIQUE (a
    per-tenant upsert), the tenant-leading index, and the same fail-closed RLS policy
    the 0007 backstop uses (``tenant_tool_policy`` is tenant-scoped, INV-1). The
    downgrade drops the policy, disables RLS, drops the index, and drops the table.
    Offline DDL render (Postgres dialect) — structural reversibility without a DB
    (#70 lesson); the behavioural proof is the tool-policy service + gate tests.
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0020_mcp_servers:0021_tenant_tool_policy", sql=True)
    up = capsys.readouterr().out.lower()
    # The tenant-scoped policy table + its FKs.
    assert "create table tenant_tool_policy" in up
    assert "references tenants" in up  # tenant-scoped (INV-1)
    assert "references users" in up  # updated_by FK (the admin, SET NULL)
    assert "tool_name" in up
    assert "requires_approval" in up
    # The per-tenant-per-tool upsert UNIQUE.
    assert "uq_tenant_tool_policy_tenant_tool" in up
    # Tenant-leading index (the INV-1 predicate column).
    assert (
        "create index ix_tenant_tool_policy_tenant_id on tenant_tool_policy (tenant_id)" in up
    )
    # The RLS backstop — same fail-closed GUC policy as 0007.
    assert "alter table tenant_tool_policy enable row level security" in up
    assert "alter table tenant_tool_policy force row level security" in up
    assert "create policy rls_tenant_tool_policy on tenant_tool_policy" in up
    assert "current_setting('app.tenant_id', true)" in up

    command.downgrade(cfg, "0021_tenant_tool_policy:0020_mcp_servers", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop policy if exists rls_tenant_tool_policy on tenant_tool_policy" in down
    assert "alter table tenant_tool_policy disable row level security" in down
    assert "drop index ix_tenant_tool_policy_tenant_id" in down
    assert "drop table tenant_tool_policy" in down


def test_offline_tenant_sandbox_policy_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0022 creates the tenant-scoped ``tenant_sandbox_policy`` table + RLS; down() reverses (#233).

    AC (issue #233, spec 0004 §2.1/§2.5): the upgrade renders the ``tenant_sandbox_policy``
    table with its ``enabled`` + package/egress + runtime/memory/quota columns, the
    ``updated_by`` FK → ``users`` (SET NULL — a deprovisioned admin does not
    cascade-delete the tenant's live policy), the per-tenant UNIQUE (a per-tenant
    singleton), the positive-cap CHECKs, the tenant-leading index, and the same
    fail-closed RLS policy the 0007 backstop uses (``tenant_sandbox_policy`` is
    tenant-scoped, INV-1). The downgrade drops the policy, disables RLS, drops the index,
    and drops the table. Offline DDL render (Postgres dialect) — structural reversibility
    without a DB (#70 lesson); the behavioural proof is the sandbox policy service +
    admission tests.
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0021_tenant_tool_policy:0022_sandbox_policy", sql=True)
    up = capsys.readouterr().out.lower()
    # The tenant-scoped policy table + its FKs.
    assert "create table tenant_sandbox_policy" in up
    assert "references tenants" in up  # tenant-scoped (INV-1)
    assert "references users" in up  # updated_by FK (the admin, SET NULL)
    assert "enabled" in up
    assert "egress_allowed" in up
    assert "max_runtime_s" in up
    # The per-tenant singleton UNIQUE + the positive-cap CHECKs.
    assert "uq_tenant_sandbox_policy_tenant" in up
    assert "ck_tenant_sandbox_policy_runtime_pos" in up
    # Tenant-leading index (the INV-1 predicate column).
    assert (
        "create index ix_tenant_sandbox_policy_tenant_id on tenant_sandbox_policy (tenant_id)"
        in up
    )
    # The RLS backstop — same fail-closed GUC policy as 0007.
    assert "alter table tenant_sandbox_policy enable row level security" in up
    assert "alter table tenant_sandbox_policy force row level security" in up
    assert "create policy rls_tenant_sandbox_policy on tenant_sandbox_policy" in up
    assert "current_setting('app.tenant_id', true)" in up

    command.downgrade(cfg, "0022_sandbox_policy:0021_tenant_tool_policy", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop policy if exists rls_tenant_sandbox_policy on tenant_sandbox_policy" in down
    assert "alter table tenant_sandbox_policy disable row level security" in down
    assert "drop index ix_tenant_sandbox_policy_tenant_id" in down
    assert "drop table tenant_sandbox_policy" in down


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


def test_offline_tenant_autonomy_policy_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0023 creates the ``tenant_autonomy_policy`` table + RLS; down() reverses (#218).

    AC (issue #218, spec 0004 §2.1/§2.5): the upgrade renders the ``tenant_autonomy_policy``
    table with its ``max_autonomy`` column, the ``updated_by`` FK → ``users`` (SET NULL
    — a deprovisioned admin does not cascade-delete the tenant's live cap), the
    per-tenant UNIQUE (a per-tenant singleton), the enum CHECK (no free-text ceiling),
    the tenant-leading index, and the same fail-closed RLS policy the 0007 backstop uses
    (``tenant_autonomy_policy`` is tenant-scoped, INV-1). The downgrade drops the policy,
    disables RLS, drops the index, and drops the table. Offline DDL render (Postgres
    dialect) — structural reversibility without a DB (#70 lesson); the behavioural proof
    is the autonomy policy service + runner autonomy tests.
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0023_assistant_governance:0024_autonomy_policy", sql=True)
    up = capsys.readouterr().out.lower()
    # The tenant-scoped cap table + its FKs.
    assert "create table tenant_autonomy_policy" in up
    assert "references tenants" in up  # tenant-scoped (INV-1)
    assert "references users" in up  # updated_by FK (the admin, SET NULL)
    assert "max_autonomy" in up
    # The per-tenant singleton UNIQUE + the enum CHECK (no free-text ceiling).
    assert "uq_tenant_autonomy_policy_tenant" in up
    assert "ck_tenant_autonomy_policy_max_autonomy" in up
    # Tenant-leading index (the INV-1 predicate column).
    assert (
        "create index ix_tenant_autonomy_policy_tenant_id on tenant_autonomy_policy (tenant_id)"
        in up
    )
    # The RLS backstop — same fail-closed GUC policy as 0007.
    assert "alter table tenant_autonomy_policy enable row level security" in up
    assert "alter table tenant_autonomy_policy force row level security" in up
    assert "create policy rls_tenant_autonomy_policy on tenant_autonomy_policy" in up
    assert "current_setting('app.tenant_id', true)" in up

    command.downgrade(cfg, "0024_autonomy_policy:0023_assistant_governance", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop policy if exists rls_tenant_autonomy_policy on tenant_autonomy_policy" in down
    assert "alter table tenant_autonomy_policy disable row level security" in down
    assert "drop index ix_tenant_autonomy_policy_tenant_id" in down
    assert "drop table tenant_autonomy_policy" in down


def test_offline_user_settings_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0029 adds ``user_preferences.custom_instructions`` + ``users.avatar_key``; down() reverses.

    AC (user settings): the upgrade renders the nullable ``custom_instructions`` TEXT
    column on ``user_preferences`` and the nullable ``avatar_key`` column on ``users``;
    the downgrade drops both. Both are plain add/drop-column steps on existing tables —
    no RLS policy change (the parent ``users`` table is outside the backstop, and adding
    a column to ``user_preferences`` does not alter its 0009 policy). Offline DDL render
    (Postgres dialect) — structural reversibility without a DB (#70 lesson).
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0028_llm_providers:0029_user_settings", sql=True)
    up = capsys.readouterr().out.lower()
    assert "alter table user_preferences add column custom_instructions" in up
    assert "alter table users add column avatar_key" in up

    command.downgrade(cfg, "0029_user_settings:0028_llm_providers", sql=True)
    down = capsys.readouterr().out.lower()
    assert "alter table users drop column avatar_key" in down
    assert "alter table user_preferences drop column custom_instructions" in down


def test_offline_toolinv_trace_migrations_round_trip(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0030 adds ``result_summary``; 0031 adds ``ordinal`` + the message index; both reverse.

    The executable reversibility mechanism for the #377/#397 trace columns
    (backend/AGENTS.md "Data & migrations"): the upgrades render the nullable
    ``result_summary``, the NOT NULL DEFAULT 0 ``ordinal``, and the
    ``(tenant_id, message_id)`` hydration index; the downgrades drop them in
    reverse. Offline DDL render (Postgres dialect) — no DB needed (#70 lesson).
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0029_user_settings:0031_toolinv_ordinal_msg_idx", sql=True)
    up = capsys.readouterr().out.lower()
    assert "alter table tool_invocations add column result_summary" in up
    assert "alter table tool_invocations add column ordinal" in up
    assert "not null" in up
    assert "ix_tool_invocations_tenant_message" in up

    command.downgrade(cfg, "0031_toolinv_ordinal_msg_idx:0029_user_settings", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop index ix_tool_invocations_tenant_message" in down
    assert "alter table tool_invocations drop column ordinal" in down
    assert "alter table tool_invocations drop column result_summary" in down


def test_offline_llm_usage_migration_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0032 creates ``llm_usage`` with its structure; the downgrade reverses it (#409).

    The executable reversibility + structure mechanism (backend/AGENTS.md; #419
    review — the head-pin alone left the table's shape untested). Offline DDL
    render (Postgres dialect) asserts every load-bearing property: the FKs +
    ON DELETE behaviour, the non-negative CHECK, the three tenant-leading
    indexes, the PARTIAL unique on ``(tenant_id, message_id)`` that makes "one
    row per answer" structural, and the ENABLE/FORCE RLS policy consistent with
    the 0007 backstop. Downgrade drops the policy, disables RLS, and drops the
    table — no DB needed (#70 lesson).
    """
    from alembic import command

    cfg = _alembic_config("postgresql+asyncpg://u:p@localhost/db")
    command.upgrade(cfg, "0031_toolinv_ordinal_msg_idx:0032_llm_usage", sql=True)
    up = capsys.readouterr().out.lower()
    assert "create table llm_usage" in up
    # Immediate (non-deferred) message FK + SET NULL; session SET NULL; tenant CASCADE.
    assert "references messages" in up
    assert "references chat_sessions" in up
    assert "references tenants" in up
    assert "deferrable" not in up  # #419 review — no forward ref, so no deferral
    # Non-negative token CHECK.
    assert "ck_llm_usage_tokens_nonneg" in up
    # The three read-path indexes + the partial-unique "one row per answer".
    assert "ix_llm_usage_tenant_session" in up
    assert "ix_llm_usage_tenant_created" in up
    assert "uq_llm_usage_tenant_message" in up
    assert "where message_id is not null" in up
    # RLS backstop, same shape as 0007/0013.
    assert "enable row level security" in up
    assert "force row level security" in up
    assert "create policy rls_llm_usage on llm_usage" in up

    command.downgrade(cfg, "0032_llm_usage:0031_toolinv_ordinal_msg_idx", sql=True)
    down = capsys.readouterr().out.lower()
    assert "drop policy if exists rls_llm_usage on llm_usage" in down
    assert "drop index uq_llm_usage_tenant_message" in down
    assert "drop table llm_usage" in down
