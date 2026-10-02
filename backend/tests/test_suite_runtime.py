"""Issue #655: fast scaffolding preserves hashing, isolation, and #94 cleanup."""

from __future__ import annotations

import asyncio
import gc
import os
import subprocess
import sys
import threading
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


def _cold_auth_process(
    tmp_path: Path, args: list[str], **policy: str
) -> subprocess.CompletedProcess[str]:
    # Keep the native process environment (SystemRoot/PATH on Windows), but
    # remove every application setting and pytest's seeded test identity.
    aliases = {str(field.alias or name).upper() for name, field in Settings.model_fields.items()}
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in aliases and not key.upper().startswith("PYTEST_")
    }
    env.update(
        {
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "ENVIRONMENT": "local",
            **policy,
        }
    )
    # The empty cwd prevents .env from supplying the missing service settings.
    return subprocess.run(
        [sys.executable, *args], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )


def test_cold_auth_import_needs_no_service_settings(tmp_path: Path) -> None:
    result = _cold_auth_process(
        tmp_path,
        [
            "-c",
            "from app.auth.principal import Principal; "
            "from app.auth import generate_refresh_token; "
            "from app.core.config import get_settings; "
            "assert get_settings.cache_info().currsize == 0; "
            "print(Principal.__name__, bool(generate_refresh_token()))",
        ],
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Principal True"


def test_cold_seed_help_needs_no_service_settings(tmp_path: Path) -> None:
    result = _cold_auth_process(tmp_path, ["-m", "app.auth.seed", "--help"])
    assert result.returncode == 0, result.stderr
    assert "--email" in result.stdout
    assert "--password" in result.stdout


@pytest.mark.parametrize("environment", ["local", "staging", "production"])
def test_cold_auth_import_rejects_fast_hashing_outside_test(
    tmp_path: Path, environment: str
) -> None:
    result = _cold_auth_process(
        tmp_path,
        ["-c", "import app.auth"],
        ENVIRONMENT=environment,
        TEST_FAST_PASSWORD_HASHING="true",
    )
    assert result.returncode != 0
    assert "TEST_FAST_PASSWORD_HASHING is allowed only in ENVIRONMENT=test" in result.stderr
    assert "Field required" not in result.stderr


@pytest.mark.parametrize(
    ("policy", "cost"),
    [
        ({"ENVIRONMENT": "local"}, (3, 65536, 4)),
        ({"ENVIRONMENT": "staging"}, (3, 65536, 4)),
        ({"ENVIRONMENT": "production"}, (3, 65536, 4)),
        ({"ENVIRONMENT": "test"}, (1, 8, 1)),
        ({"ENVIRONMENT": "test", "TEST_FAST_PASSWORD_HASHING": "false"}, (3, 65536, 4)),
    ],
    ids=["local", "staging", "production", "test-default", "test-opt-out"],
)
def test_cold_auth_import_selects_only_hashing_policy(
    tmp_path: Path, policy: dict[str, str], cost: tuple[int, int, int]
) -> None:
    result = _cold_auth_process(
        tmp_path,
        [
            "-c",
            "from app.auth import hashing; "
            "from argon2 import extract_parameters; "
            "p = extract_parameters(hashing._DUMMY_HASH); "
            "print((p.time_cost, p.memory_cost, p.parallelism))",
        ],
        **policy,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(cost)


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


@pytest.mark.parametrize(
    "loop_type",
    [asyncio.SelectorEventLoop] + ([asyncio.ProactorEventLoop] if sys.platform == "win32" else []),
)
def test_loop_tracker_defers_concurrent_construction(
    monkeypatch: pytest.MonkeyPatch, loop_type: type[asyncio.BaseEventLoop]
) -> None:
    """R1-001: teardown must not close a loop before its self-pipe exists."""
    tracker = LoopTracker()
    tracker.install(monkeypatch)
    constructing = threading.Event()
    finish_construction = threading.Event()
    created: list[asyncio.BaseEventLoop] = []
    original = loop_type._make_self_pipe

    def blocked_self_pipe(loop: asyncio.BaseEventLoop) -> None:
        constructing.set()
        assert finish_construction.wait(60), "constructor handshake stalled"
        original(loop)

    monkeypatch.setattr(loop_type, "_make_self_pipe", blocked_self_pipe)
    thread = threading.Thread(target=lambda: created.append(loop_type()))
    thread.start()
    try:
        assert constructing.wait(60), "constructor never reached self-pipe handshake"
        tracker.close_idle()
    finally:
        finish_construction.set()
        thread.join(60)
        assert not thread.is_alive(), "constructor did not finish"
        tracker.close_idle()
    assert len(created) == 1
    assert created[0].is_closed()
    assert not tracker.loops


@pytest.mark.parametrize("stop_before_close_returns", [False, True])
def test_loop_tracker_defers_loop_started_during_teardown(
    monkeypatch: pytest.MonkeyPatch, stop_before_close_returns: bool
) -> None:
    """An idle check is only a snapshot; close may lose a race with run_forever."""
    tracker = LoopTracker()
    tracker.install(monkeypatch)
    loop = asyncio.new_event_loop()
    start_loop = threading.Event()
    running = threading.Event()
    original = loop.is_running

    def stale_idle_check() -> bool:
        was_running = original()
        if not start_loop.is_set():
            start_loop.set()
            assert running.wait(60), "loop never started"
        return was_running

    def run() -> None:
        assert start_loop.wait(60), "teardown never checked idle state"
        loop.call_soon(running.set)
        loop.run_forever()

    thread = threading.Thread(target=run)
    thread.start()
    monkeypatch.setattr(loop, "is_running", stale_idle_check)
    original_close = loop.close

    def raced_close() -> None:
        try:
            original_close()
        except RuntimeError:
            if stop_before_close_returns:
                loop.call_soon_threadsafe(loop.stop)
                thread.join(60)
                assert not thread.is_alive(), "loop did not stop before close returned"
            raise

    monkeypatch.setattr(loop, "close", raced_close)
    try:
        tracker.close_idle()
        assert not loop.is_closed()
        assert loop in tracker.loops
    finally:
        start_loop.set()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(60)
        assert not thread.is_alive(), "loop thread did not stop"
        monkeypatch.setattr(loop, "is_running", original)
        tracker.close_idle()
    assert loop.is_closed()


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
