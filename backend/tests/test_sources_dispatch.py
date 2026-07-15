"""The #271 off-loop enqueue dispatcher — its own module.

Deliberately SEPARATE from test_sources_service.py: that module's autouse
fixture pins ``_dispatch_off_loop`` to run inline (so the enqueue-recorder
assertions stay deterministic), which would defeat these tests of the real
dispatcher.
"""

from __future__ import annotations

import asyncio

import pytest

# --- #271: the enqueue dispatch runs OFF the event loop ----------------------


async def test_dispatch_off_loop_runs_on_another_thread() -> None:
    """With a loop running (the request path), the blocking enqueue must not
    execute on the loop thread — a Redis/broker blip would stall every request."""
    import threading

    from app.services.sources_service import _dispatch_off_loop

    done = threading.Event()
    seen: dict[str, int] = {}

    def fn() -> None:
        seen["tid"] = threading.get_ident()
        done.set()

    _dispatch_off_loop(fn, name="test-dispatch")
    assert await asyncio.to_thread(done.wait, 2.0), "dispatched fn never ran"
    assert seen["tid"] != threading.get_ident()


async def test_dispatch_off_loop_swallows_and_LOGS_enqueue_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing enqueue is best-effort: never propagated, always logged.

    The log assertion is the point (401 review): without it, removing the
    try/except would still pass because the executor future is discarded.
    The event name and error TYPE are asserted; raw arguments never reach the
    log call.
    """
    import threading

    from app.services import sources_service
    from app.services.sources_service import _dispatch_off_loop

    logged = threading.Event()
    records: list[tuple[str, dict[str, object]]] = []

    class _Log:
        def warning(self, event: str, **kw: object) -> None:
            records.append((event, kw))
            logged.set()

    monkeypatch.setattr(sources_service, "log", _Log())

    def fn() -> None:
        raise RuntimeError("broker down — with sensitive-arg detail")

    _dispatch_off_loop(fn, name="test-dispatch")  # must not raise, ever
    assert await asyncio.to_thread(logged.wait, 2.0), "failure was never logged"
    event, kw = records[0]
    assert event == "enqueue.dispatch_failed"
    assert kw["dispatch"] == "test-dispatch"
    assert kw["error"] == "RuntimeError"
    # The error MESSAGE (which may carry args/URLs) stays out of the log call.
    assert "sensitive" not in str(kw)


async def test_dispatch_off_loop_carries_request_contextvars() -> None:
    """The executor hop preserves contextvars (401 review): the correlation ids
    CorrelationMiddleware binds must reach logs emitted inside the dispatched
    enqueue, or incident logs are orphaned exactly when they matter."""
    import contextvars
    import threading

    from app.services.sources_service import _dispatch_off_loop

    request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
        "request_id_test", default=None
    )
    request_id.set("req-42")

    seen: dict[str, str | None] = {}
    done = threading.Event()

    def fn() -> None:
        seen["request_id"] = request_id.get()
        done.set()

    _dispatch_off_loop(fn, name="ctx-test")
    assert await asyncio.to_thread(done.wait, 2.0)
    assert seen["request_id"] == "req-42"


async def test_dispatch_off_loop_logs_a_rejected_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An executor rejecting the submission (shutdown) is logged, not raised —
    the docstring's never-propagate guarantee covers the submission itself."""
    import asyncio as aio

    from app.services import sources_service
    from app.services.sources_service import _dispatch_off_loop

    records: list[tuple[str, dict[str, object]]] = []

    class _Log:
        def warning(self, event: str, **kw: object) -> None:
            records.append((event, kw))

    monkeypatch.setattr(sources_service, "log", _Log())

    loop = aio.get_running_loop()

    def _reject(*a: object, **k: object) -> None:
        raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(loop, "run_in_executor", _reject)
    _dispatch_off_loop(lambda: None, name="rejected")  # must not raise
    assert records and records[0][0] == "enqueue.dispatch_rejected"
