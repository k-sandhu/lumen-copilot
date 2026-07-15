"""The #271 off-loop enqueue dispatcher — its own module.

Deliberately SEPARATE from test_sources_service.py: that module's autouse
fixture pins ``_dispatch_off_loop`` to run inline (so the enqueue-recorder
assertions stay deterministic), which would defeat these tests of the real
dispatcher.
"""

from __future__ import annotations

import asyncio

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


async def test_dispatch_off_loop_swallows_and_logs_enqueue_errors() -> None:
    """A failing enqueue is best-effort: it must not propagate anywhere."""
    import threading

    from app.services.sources_service import _dispatch_off_loop

    done = threading.Event()

    def fn() -> None:
        done.set()
        raise RuntimeError("broker down")

    _dispatch_off_loop(fn, name="test-dispatch")  # must not raise, ever
    assert await asyncio.to_thread(done.wait, 2.0)

