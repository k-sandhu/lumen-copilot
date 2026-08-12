"""Targeted disposable-Postgres proof for durable denial auditing (#579).

This is intentionally opt-in via ``AUDIT_DENIAL_LIVE_DATABASE_URL`` rather
than the repository-wide live switch. It creates a random database, migrates it,
proves an independently committed denial survives while a flushed caller write
rolls back, reads the safe event back, and drops the database in ``finally``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.repositories import (
    AuditEventRepository,
    CollectionRepository,
    TenantRepository,
    UserRepository,
)
from app.db.tenant_context import bind_bypass, bind_tenant
from app.domain.entities import Role
from app.services.audit import PermissionDeniedRecorder

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_BASE_URL = os.environ.get("AUDIT_DENIAL_LIVE_DATABASE_URL")


def _swap_db(url: str, dbname: str) -> str:
    return urlunparse(urlparse(url)._replace(path=f"/{dbname}"))


@pytest.mark.skipif(
    _BASE_URL is None,
    reason="Set AUDIT_DENIAL_LIVE_DATABASE_URL for the targeted disposable-Postgres proof.",
)
async def test_durable_denial_isolated_transaction_live() -> None:
    """A denial commits alone; unrelated flushed caller work still rolls back."""
    assert _BASE_URL is not None
    from alembic import command

    from app.core.config import get_settings

    database_name = f"lumen_audit579_{uuid.uuid4().hex[:12]}"
    admin_url = _swap_db(_BASE_URL, "postgres")
    database_url = _swap_db(_BASE_URL, database_name)
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    finally:
        await admin.dispose()

    prior_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    get_settings.cache_clear()
    engine = create_async_engine(database_url)
    try:
        config = Config(str(_BACKEND_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
        await asyncio.to_thread(command.upgrade, config, "head")

        factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        async with factory() as seed:
            await bind_bypass(seed)
            tenant = await TenantRepository(seed).create(name="Audit 579 disposable tenant")
            actor = await UserRepository(seed, tenant.id).create(
                email="actor@audit579.invalid",
                password_hash="disposable-not-a-credential",
                roles=[Role.MEMBER],
            )
            await seed.commit()

        request_id = f"req-audit579-{uuid.uuid4()}"
        async with factory() as caller:
            await bind_tenant(caller, tenant.id)
            pending = await CollectionRepository(caller, tenant.id).create(
                owner_id=actor.id,
                name="must roll back",
            )
            recorder = PermissionDeniedRecorder(caller, tenant_id=tenant.id)
            await recorder.emit(
                actor_id=actor.id,
                resource_type="assistant",
                resource_id=str(uuid.uuid4()),
                attempted_action="assistant.read",
                reason="not_visible",
                request_id=request_id,
                source_ip="203.0.113.79",
            )
            await caller.rollback()

        async with factory() as readback:
            await bind_tenant(readback, tenant.id)
            assert await CollectionRepository(readback, tenant.id).get(pending.id) is None
            matching = [
                event
                for event in await AuditEventRepository(readback, tenant.id).list_recent(limit=20)
                if event.request_id == request_id
            ]
            assert len(matching) == 1
            denial = matching[0]
            assert denial.actor_id == actor.id
            assert denial.action == "permission.denied"
            assert denial.outcome.value == "denied"
            assert denial.source_origin == "client"
            assert str(denial.source_ip) == "203.0.113.79"
            assert denial.metadata == {
                "attempted_action": "assistant.read",
                "reason": "not_visible",
            }
    finally:
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
        finally:
            await admin.dispose()
