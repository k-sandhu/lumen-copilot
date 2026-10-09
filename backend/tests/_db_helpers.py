"""One empty SQLite schema per worker; independent copies on existing engines."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
from sqlalchemy import Connection, create_engine

from app.db.base import Base

_template: Path | None = None


def build_sqlite_template(path: Path) -> None:
    """Called by the session fixture after collection registered every model."""
    global _template
    import app.db.models  # noqa: F401 — register the complete schema

    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    _template = path


def copy_sqlite_schema(connection: Connection) -> None:
    """SQLAlchemy ``run_sync`` callback; preserve the caller's pool and hooks.

    SQLite's backup copies pages, indexes and constraints into the existing
    connection, including in-memory StaticPool engines. The source is always
    empty and opened read-only. Non-SQLite live fixtures still execute real DDL.
    """
    if connection.dialect.name != "sqlite":
        Base.metadata.create_all(connection)
        return
    assert _template is not None, "SQLite template session fixture must run first"
    uri = _template.as_uri() + "?mode=ro"

    async def restore(target: aiosqlite.Connection) -> None:
        async with aiosqlite.connect(uri, uri=True) as source:
            await source.backup(target)

    connection.connection.dbapi_connection.run_async(restore)
