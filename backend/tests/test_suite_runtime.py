"""Issue #655: fast scaffolding preserves hashing, isolation, and #94 cleanup."""

from __future__ import annotations

import asyncio
import gc
from pathlib import Path

import pytest
from argon2 import PasswordHasher, Type, extract_parameters
from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth import hashing
from app.core.config import Settings
from app.db.base import Base
from tests._db_helpers import copy_sqlite_schema
from tests._live_helpers import isolated_live_url, worker_database_name
from tests._loop_helpers import LoopTracker
from tests.test_auth_config import _BASE, _PROD_OVERRIDES


def _settings(environment: str, **overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        **{**_BASE, **_PROD_OVERRIDES, "ENVIRONMENT": environment, **overrides},
    )


@pytest.mark.parametrize("environment", ["production", "local", "staging"])
def test_fast_hashing_refused_outside_test(environment: str) -> None:
    with pytest.raises(ValidationError, match="TEST_FAST_PASSWORD_HASHING.*test"):
        _settings(environment, TEST_FAST_PASSWORD_HASHING=True)


@pytest.mark.parametrize("environment", ["production", "local", "staging"])
def test_production_hashing_parameters_unchanged(environment: str) -> None:
    hasher = hashing._build_hasher(_settings(environment))
    assert (hasher.time_cost, hasher.memory_cost, hasher.parallelism) == (3, 65536, 4)
    assert (hasher.hash_len, hasher.salt_len, hasher.type) == (32, 16, Type.ID)
    assert hasher._parameters == PasswordHasher(type=Type.ID)._parameters


def test_fast_hashes_verify_and_reject_wrong_password() -> None:
    hasher = hashing._build_hasher(_settings("test"))
    encoded = hasher.hash("correct horse battery staple")
    assert (hasher.time_cost, hasher.memory_cost, hasher.parallelism) == (1, 8, 1)
    assert hasher.verify(encoded, "correct horse battery staple")
    assert hashing.verify_password(encoded, "correct horse battery staple")
    assert not hashing.verify_password(encoded, "wrong")
    # Parameters travel in the PHC string: the production verifier still works.
    assert PasswordHasher(type=Type.ID).verify(encoded, "correct horse battery staple")


def test_import_time_dummy_hash_uses_test_cost() -> None:
    parameters = extract_parameters(hashing._DUMMY_HASH)
    assert (parameters.time_cost, parameters.memory_cost, parameters.parallelism) == (1, 8, 1)
    hashing.dummy_verify()


def test_test_environment_can_explicitly_keep_production_hashing() -> None:
    hasher = hashing._build_hasher(_settings("test", TEST_FAST_PASSWORD_HASHING=False))
    assert hasher.memory_cost == 65536


def test_hasher_defends_against_bypassed_settings_validation() -> None:
    settings = _settings("test")
    settings.environment = "production"
    assert settings.test_fast_password_hashing is True
    assert hashing._build_hasher(settings).memory_cost == 65536


@pytest.mark.parametrize("worker", ["", "gw0", "gw1"])
def test_live_database_names_are_worker_isolated(worker: str) -> None:
    name = worker_database_name("lumentest_pr655", worker)
    assert name == "lumentest_pr655" + (f"_{worker}" if worker else "")
    assert worker_database_name(name, worker) == name


def test_live_url_never_selects_app_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/lumen")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    assert isolated_live_url("postgresql+asyncpg://u:p@localhost/lumentest_pr655").endswith(
        "/lumentest_pr655_gw1"
    )


def test_live_url_rejects_non_disposable_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="disposable"):
        isolated_live_url("postgresql+asyncpg://u:p@localhost/lumen")


@pytest.mark.parametrize("worker", ["../../outside", "gw1;DROP", "other"])
def test_live_worker_identifier_rejects_unsafe_values(worker: str) -> None:
    with pytest.raises(ValueError, match="worker"):
        worker_database_name("lumentest_pr655", worker)


async def test_schema_copies_match_ddl_and_isolate_rows_and_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engines = [create_async_engine(f"sqlite+aiosqlite:///{tmp_path / f'{i}.db'}") for i in range(3)]
    try:
        async with engines[0].begin() as conn:
            # Deliberately exercise fresh DDL as the reference for this test.
            await conn.run_sync(Base.metadata.create_all)
            reference = await conn.run_sync(lambda sync: inspect(sync).get_table_names())

        def unexpected_ddl(*args: object, **kwargs: object) -> None:
            pytest.fail("schema copies must not rebuild metadata")

        monkeypatch.setattr(Base.metadata, "create_all", unexpected_ddl)
        for engine in engines[1:]:
            async with engine.begin() as conn:
                await conn.run_sync(copy_sqlite_schema)
                assert (
                    await conn.run_sync(lambda sync: inspect(sync).get_table_names()) == reference
                )
        async with engines[1].begin() as conn:
            await conn.execute(text("CREATE TABLE isolation_probe (value INTEGER UNIQUE)"))
            await conn.execute(text("INSERT INTO isolation_probe VALUES (1)"))
            await conn.execute(text("DROP TABLE users"))
        async with engines[2].connect() as conn:
            tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
            assert "users" in tables
            assert "isolation_probe" not in tables
            assert (await conn.execute(text("SELECT COUNT(*) FROM tenants"))).scalar_one() == 0
    finally:
        for engine in engines:
            await engine.dispose()


def test_loop_tracker_closes_idle_loops_without_heap_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = LoopTracker()
    tracker.install(monkeypatch)
    loops = [asyncio.new_event_loop(), asyncio.get_event_loop_policy().new_event_loop()]
    # Retain a cyclic orphan, exactly the class of delayed socket warning in #94.
    loops[0].call_soon(lambda loop=loops[0]: loop)
    monkeypatch.setattr(gc, "collect", lambda: pytest.fail("per-test GC is forbidden"))
    monkeypatch.setattr(gc, "get_objects", lambda: pytest.fail("heap scan is forbidden"))
    tracker.close_idle()
    assert all(loop.is_closed() for loop in loops)
    assert not tracker.loops


async def test_loop_tracker_keeps_running_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = LoopTracker()
    loop = asyncio.get_running_loop()
    tracker.loops.add(loop)
    tracker.close_idle()
    assert not loop.is_closed()
    assert loop in tracker.loops


def test_real_autouse_teardown_closes_cyclic_orphan_before_next_test() -> None:
    from tests.conftest import _close_orphan_event_loops

    # Drive the actual fixture's setup/teardown boundary without starting a
    # second pytest interpreter (the shared-machine RAM contract forbids that).
    teardown = _close_orphan_event_loops.__wrapped__()
    next(teardown)
    loop = asyncio.new_event_loop()
    loop.call_soon(lambda captured=loop: captured)
    assert not loop.is_closed()
    with pytest.raises(StopIteration):
        next(teardown)
    assert loop.is_closed(), "orphan survived the real autouse teardown boundary"
    del loop
    gc.collect()  # emulate the following test's GC; -W error must stay green
