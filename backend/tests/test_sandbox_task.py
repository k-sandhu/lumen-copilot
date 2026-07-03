"""Sandbox Celery task tests — the sync wrapper + enqueue seam (ADR-0013 §4, #230).

Offline: the wrapper is exercised with the async core patched (so no runner / DB is
touched), and the enqueue seam with a faked broker connection, so the crash-to-failed
branch and the broker-outage-swallow branch are covered fully offline (no real
broker, no datastore).
"""

from __future__ import annotations

import uuid

import pytest
from kombu.exceptions import OperationalError

import app.tasks.run_sandbox as sandbox_module
from app.domain.entities import CodeRunStatus
from app.tasks.celery_app import celery_app
from app.tasks.run_sandbox import enqueue_code_run, run_sandbox

# Celery binds ``self`` to the task singleton; ``.__wrapped__.__func__`` is the raw
# function so we can call it with a fake ``self`` (mirrors the ingestion task test).
_run_sandbox_wrapper = run_sandbox.__wrapped__.__func__  # type: ignore[attr-defined]


class _FakeSelf:
    """A stand-in for Celery's bound ``self`` (the task needs no request here)."""


def test_wrapper_success_returns_terminal_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sync wrapper returns the terminal status the core produced."""

    async def _ok(cid: uuid.UUID, tid: uuid.UUID, *, settings: object) -> CodeRunStatus:
        return CodeRunStatus.SUCCEEDED

    monkeypatch.setattr(sandbox_module, "execute_code_run_async", _ok)
    monkeypatch.setattr(sandbox_module, "get_settings", lambda: object())

    out = _run_sandbox_wrapper(_FakeSelf(), str(uuid.uuid4()), str(uuid.uuid4()))
    assert out["status"] == "succeeded"


def test_wrapper_crash_reports_failed_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """A defect that escapes the core is caught: the wrapper reports ``failed``, never raises.

    The core is crash-safe (it writes a terminal itself); if the wrapper still sees an
    exception (e.g. loop setup), it logs the type only and acknowledges the message
    rather than redelivering forever (ADR-0013 §5).
    """

    async def _boom(cid: uuid.UUID, tid: uuid.UUID, *, settings: object) -> CodeRunStatus:
        raise RuntimeError("loop blew up")

    monkeypatch.setattr(sandbox_module, "execute_code_run_async", _boom)
    monkeypatch.setattr(sandbox_module, "get_settings", lambda: object())

    out = _run_sandbox_wrapper(_FakeSelf(), str(uuid.uuid4()), str(uuid.uuid4()))
    assert out["status"] == "failed"


class _FakeConnection:
    def __init__(self, *, raise_on_ensure: Exception | None = None) -> None:
        self.raise_on_ensure = raise_on_ensure

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def ensure_connection(self, **_kwargs: object) -> None:
        if self.raise_on_ensure is not None:
            raise self.raise_on_ensure


def test_enqueue_swallows_broker_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broker ``OperationalError`` is logged and swallowed — the enqueue never raises."""
    conn = _FakeConnection(raise_on_ensure=OperationalError("broker unreachable"))
    monkeypatch.setattr(celery_app, "connection_for_write", lambda: conn)

    published: list[object] = []
    monkeypatch.setattr(
        run_sandbox, "apply_async", lambda *a, **k: published.append((a, k))
    )

    # Must not raise; nothing is published on the unreachable broker.
    enqueue_code_run(uuid.uuid4(), uuid.uuid4())
    assert published == []
