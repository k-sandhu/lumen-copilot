"""Execute the metadata migration against existing rows, without ORM create_all."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory


def test_discovery_metadata_migration_round_trip() -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    revision = script.get_revision("0049_discovery_metadata")
    assert revision is not None
    assert revision.down_revision == "0048_evidence_handles"
    module = revision.module
    fields = {"title", "source_path", "source_modified_at", "discovery_metadata"}

    sql = StringIO()
    with Operations.context(
        MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": sql}
        )
    ):
        module.upgrade()
        module.downgrade()
    ddl = sql.getvalue().lower()
    for field in fields:
        assert f"add column {field}" in ddl
        assert f"drop column {field}" in ddl
    assert "timestamp with time zone" in ddl
    assert "jsonb" in ddl

    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE documents (id INTEGER PRIMARY KEY, filename VARCHAR(512) NOT NULL)"
            )
            connection.exec_driver_sql("INSERT INTO documents VALUES (1, 'existing.txt')")
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
            columns = {
                column["name"]: column for column in sa.inspect(connection).get_columns("documents")
            }
            assert fields <= columns.keys()
            assert all(columns[field]["nullable"] for field in fields)
            assert tuple(
                connection.exec_driver_sql(
                    "SELECT filename, title, source_path, source_modified_at, discovery_metadata "
                    "FROM documents"
                ).one()
            ) == ("existing.txt", None, None, None, None)
            connection.exec_driver_sql(
                "UPDATE documents SET title='Plan', discovery_metadata='{}' WHERE id=1"
            )
            assert (
                connection.exec_driver_sql("SELECT title FROM documents WHERE id=1").scalar_one()
                == "Plan"
            )
            with Operations.context(MigrationContext.configure(connection)):
                module.downgrade()
            assert {
                column["name"] for column in sa.inspect(connection).get_columns("documents")
            } == {"id", "filename"}
            assert (
                connection.exec_driver_sql("SELECT filename FROM documents").scalar_one()
                == "existing.txt"
            )
    finally:
        engine.dispose()
