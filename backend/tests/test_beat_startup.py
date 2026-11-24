"""Exercise the composed Celery Beat lifecycle, before scheduler construction/ticks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog
from celery.beat import Service
from redbeat import RedBeatSchedulerEntry
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.repositories import (
    AssistantRepository,
    ScheduleRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import AutonomyLevel, KnowledgeScope, Role
from app.domain.scheduling import cadence_from_cron
from app.tasks import scheduler
from app.tasks.celery_app import celery_app
from tests._db_helpers import copy_sqlite_schema


def _start_beat(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    """Use the real Beat.run -> Service.start; replace only transport/ticks."""

    class OneTickService(Service):
        def get_scheduler(self, *args: object, **kwargs: object) -> MagicMock:
            events.append("construct")
            settings = get_settings()
            assert set(self.app.conf.beat_schedule) == {
                "expired-upload-sweep",
                "run-digest-sweep",
                "connector-sync-poll",
                "stale-run-reaper",
            }
            assert self.app.conf.redbeat_key_prefix == settings.redbeat_key_prefix
            assert self.app.conf.redbeat_lock_timeout == settings.redbeat_lock_timeout_seconds
            assert self.app.conf.redbeat_redis_url == settings.redis_url
            assert self.app.conf.beat_schedule == {
                "expired-upload-sweep": {
                    "task": "lumen.sweep_expired_uploads",
                    "schedule": float(settings.upload_janitor_interval_seconds),
                },
                "run-digest-sweep": {
                    "task": "lumen.build_run_digests",
                    "schedule": float(settings.run_digest_interval_seconds),
                },
                "connector-sync-poll": {
                    "task": "lumen.poll_connector_syncs",
                    "schedule": float(settings.connector_sync_interval_minutes) * 60,
                },
                "stale-run-reaper": {
                    "task": "lumen.reap_stale_runs",
                    "schedule": float(settings.run_reaper_interval_seconds),
                },
            }
            fake = MagicMock(max_interval=1)

            def tick() -> int:
                events.append("tick")
                self._is_shutdown.set()
                return 0

            fake.tick.side_effect = tick
            return fake

    monkeypatch.setattr(celery_app, "finalized", False)
    beat = celery_app.Beat(quiet=True, scheduler="redbeat.RedBeatScheduler")
    monkeypatch.setattr(beat, "Service", OneTickService)
    monkeypatch.setattr(beat, "set_process_title", lambda: None)
    monkeypatch.setattr(beat, "setup_logging", lambda: None)
    monkeypatch.setattr(beat, "install_sync_handler", lambda service: None)
    beat.run()


def test_production_beat_configures_then_reconciles_before_first_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def reconcile(settings: object = None) -> int:
        events.append("reconcile")
        return 2

    monkeypatch.setattr(scheduler, "reconcile_from_postgres", reconcile)
    _start_beat(monkeypatch, events)
    _start_beat(monkeypatch, events)
    assert events == ["construct", "reconcile", "tick"] * 2


def test_production_beat_reconcile_failure_exits_before_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    logger = MagicMock()
    monkeypatch.setattr(structlog, "get_logger", lambda *args: logger)
    monkeypatch.setattr(
        scheduler, "reconcile_from_postgres", MagicMock(side_effect=RuntimeError("database down"))
    )
    with pytest.raises(SystemExit) as raised:
        _start_beat(monkeypatch, events)
    assert raised.value.code == 1
    assert events == ["construct"]
    logger.error.assert_called_once_with(
        "schedule.boot_reconcile_failed", error_type="RuntimeError"
    )
    logger.info.assert_not_called()


def test_startup_projects_enabled_persisted_rows_once_and_exposes_projection_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'beat.db'}", poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session.get_sessionmaker", lambda settings=None: factory)

    async def seed() -> set[tuple[str, str]]:
        async with engine.begin() as conn:
            await conn.run_sync(copy_sqlite_schema)
        expected = set()
        async with factory() as session:
            for name in ["tenant-a", "tenant-b"]:
                tenant = await TenantRepository(session).create(name=name)
                owner = await UserRepository(session, tenant.id).create(
                    email=f"owner@{name}.test", password_hash="x", roles=[Role.MEMBER]
                )
                assistant = await AssistantRepository(session, tenant.id).create(
                    owner_id=owner.id,
                    name="Weekly",
                    knowledge_scope=KnowledgeScope.empty(),
                    tool_allowlist=(),
                    autonomy_level=AutonomyLevel.SUGGEST,
                    backup_owner_id=None,
                )
                repo = ScheduleRepository(session, tenant.id)
                for enabled in [True, False]:
                    row = await repo.create(
                        owner_id=owner.id,
                        assistant_id=assistant.id,
                        cadence=cadence_from_cron("* * * * *"),
                        timezone="UTC",
                        input_params={},
                        enabled=enabled,
                    )
                    if enabled:
                        expected.add((str(row.id), str(tenant.id)))
                deleted = await repo.create(
                    owner_id=owner.id,
                    assistant_id=assistant.id,
                    cadence=cadence_from_cron("* * * * *"),
                    timezone="UTC",
                    input_params={},
                )
                await repo.delete(deleted.id)
            await session.commit()
        return expected

    expected = asyncio.run(seed())
    entries: dict[str, tuple[str, str]] = {}

    def save(entry: RedBeatSchedulerEntry) -> None:
        entries[entry.key] = tuple(entry.args)

    monkeypatch.setattr(RedBeatSchedulerEntry, "save", save)
    _start_beat(monkeypatch, [])
    _start_beat(monkeypatch, [])
    assert set(entries.values()) == expected
    assert len(entries) == 2

    monkeypatch.setattr(RedBeatSchedulerEntry, "save", MagicMock(side_effect=ConnectionError()))
    events: list[str] = []
    with pytest.raises(SystemExit) as raised:
        _start_beat(monkeypatch, events)
    assert raised.value.code == 1
    assert events == ["construct"]
