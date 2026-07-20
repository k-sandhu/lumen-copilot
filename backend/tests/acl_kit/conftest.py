"""Kit fixtures — one seeded two-store world per connector subject.

Every enforcement module declares ``pytestmark = pytest.mark.parametrize(
"subject", SUBJECTS, ids=SUBJECT_IDS)``; the ``world`` fixture below picks that
parameter up, so a proof written once runs for every ACL-declaring connector.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.session as db_session
from app.db.base import Base

from .subject import AclSubject
from .world import World, build_world

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata


@pytest_asyncio.fixture
async def sqlite_db() -> AsyncIterator[None]:
    """Point the app's sessionmaker at a fresh in-memory database.

    ``StaticPool`` keeps every connection on the same in-memory database, which
    is what makes the repositories, the retrieval builders and the index-sync
    reader all see one corpus.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    previous_engine = db_session._engine
    previous_maker = db_session._sessionmaker
    db_session._engine = engine
    db_session._sessionmaker = async_sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False
    )
    try:
        yield
    finally:
        db_session._engine = previous_engine
        db_session._sessionmaker = previous_maker
        await engine.dispose()


@pytest_asyncio.fixture
async def world(sqlite_db: None, subject: AclSubject) -> World:
    """The seeded corpus for the subject under test, in Postgres AND the engine."""
    return await build_world(subject)


@pytest.fixture
def no_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch a real broker from a kit test (offline, CI-default)."""
    monkeypatch.setattr("app.tasks.enqueue_source_sync", lambda *a, **k: None)
    monkeypatch.setattr("app.tasks.enqueue_index_sync", lambda *a, **k: None)
