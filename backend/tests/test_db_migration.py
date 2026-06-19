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

# Every table the ORM registry now carries: the MVP set plus refresh_tokens,
# added by the 0003 migration (issue #19, spec 0004 §2.3).
_ALL_TABLES = _MVP_TABLES | {"refresh_tokens"}


def _alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    if url is not None:
        cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_metadata_covers_every_mvp_table() -> None:
    """The ORM registry and the spec-0004 table list agree."""
    assert set(Base.metadata.tables) == _ALL_TABLES


def test_migration_chain_is_linear_to_retrieval_indexes() -> None:
    """The chain is linear: 0001 → 0002 → 0003 → 0004, a single head at 0004."""
    script = ScriptDirectory.from_config(_alembic_config())
    assert list(script.get_heads()) == ["0004_retrieval_indexes"]
    mvp = script.get_revision("0002_mvp_schema")
    assert mvp is not None
    assert mvp.down_revision == "0001_enable_pgvector"
    rt = script.get_revision("0003_refresh_tokens")
    assert rt is not None
    assert rt.down_revision == "0002_mvp_schema"
    ri = script.get_revision("0004_retrieval_indexes")
    assert ri is not None
    assert ri.down_revision == "0003_refresh_tokens"


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
