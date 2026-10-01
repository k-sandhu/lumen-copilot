"""Real API/PostgreSQL held-header proof for the admission budget (#580/R4-002)."""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db_session, get_settings_dep
from app.auth import hash_password
from app.core.config import get_settings
from app.db.base import Base
from app.db.models import AuditEvent, RefreshToken, Tenant, User
from app.db.repositories import TenantRepository, UserRepository
from app.domain.entities import Role
from app.main import create_app
from tests.test_auth_api import _DEV_EMAIL, _DEV_PASSWORD, assert_held_login_cookie_budget


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="requires disposable PostgreSQL")
async def test_postgres_held_login_headers_respect_outstanding_budget() -> None:
    url = os.environ["DATABASE_URL"]
    # These tests have no authority over the shared app or another test database.
    assert "@localhost:47182/lumentest_pr603" in url
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tables = [Tenant.__table__, User.__table__, RefreshToken.__table__, AuditEvent.__table__]
    application = create_app()

    async def sessions():  # type: ignore[no-untyped-def]
        async with factory() as session:
            yield session

    application.dependency_overrides[get_db_session] = sessions
    application.dependency_overrides[get_settings_dep] = lambda: get_settings().model_copy(
        update={"auth_session_max_active": 2}
    )
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    try:
        async with factory() as seed:
            tenant = await TenantRepository(seed).create(name="R5 held headers")
            await UserRepository(seed, tenant.id).create(
                email=_DEV_EMAIL, password_hash=hash_password(_DEV_PASSWORD), roles=[Role.MEMBER]
            )
            await seed.commit()
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            await assert_held_login_cookie_budget(application, client)
        async with factory() as verify:
            assert await verify.scalar(text("SELECT count(*) FROM refresh_tokens")) == 4
            assert (
                await verify.scalar(
                    text("SELECT count(*) FROM refresh_tokens WHERE revoked_at IS NULL")
                )
                == 2
            )
            assert (
                await verify.scalar(
                    text(
                        "SELECT count(*) FROM audit_events WHERE action = 'auth.login_failed' "
                        "AND outcome = 'denied'"
                    )
                )
                == 17
            )
    finally:
        application.dependency_overrides.clear()
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.drop_all(sync, tables=list(reversed(tables)))
            )
        await engine.dispose()
