"""Alembic migration environment — async.

Runs migrations against the async engine using the database URL from
``app.core.config`` (the single env reader — no URL or secret in
``alembic.ini``). ``target_metadata`` is ``app.db.Base.metadata`` so future
autogenerate has the model registry; models are imported for their side effect
of registering on that metadata (none exist yet in the skeleton).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.base import Base

# Alembic Config object (reads alembic.ini for logging config).
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the DB URL from settings (the only env reader) at runtime.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

# Autogenerate target. Import models here as they are added so they register.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode against the async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
