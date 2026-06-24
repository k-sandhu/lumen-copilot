"""Bridge a Celery task's sync entrypoint to its async core (#140).

Celery's prefork worker invokes tasks **synchronously**, so each task drives its
``*_async`` core on a private event loop via :func:`asyncio.run`. The catch: the
process-wide async engine (:mod:`app.db.session`) binds its pooled connections to
whatever event loop is running when they are first opened. ``asyncio.run`` closes
its loop on return, so a connection pooled by one task run would, on the *next*
run, be touched from a different — now closed — loop. Under ``asyncpg`` that
raises ``RuntimeError: ... got Future ... attached to a different loop`` /
``Event loop is closed``. Worse, the failure path that should mark a document
``failed`` also writes to the DB, so it crashes too, stranding the document in
``processing`` forever (an AC-6 violation).

:func:`run_task` closes that gap with the smallest possible change: it runs the
coroutine on a fresh loop (as before) and then disposes the engine **inside that
same loop**, before the loop closes, so no pooled connection ever outlives the
loop that created it. The next task starts from a clean, lazily-rebuilt engine.

This is fix option 1 from #140 (dispose-per-run); the alternatives — a single
persistent worker loop, or ``NullPool`` — are heavier and recorded there.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import TypeVar

from app.db.session import dispose_engine

_T = TypeVar("_T")


def run_task(coro: Coroutine[object, object, _T]) -> _T:
    """Run ``coro`` on a private event loop, disposing the DB engine afterwards.

    The disposal runs in a ``finally`` *within* the loop, so the engine's pooled
    connections are closed by the loop that owns them — even when ``coro`` raises
    (a failing run must not leave a stale, loop-bound connection for the next task
    to trip over). Mirrors ``asyncio.run(coro)``'s shape and return value; callers
    pass the coroutine exactly as they did to ``asyncio.run``.
    """

    async def _runner() -> _T:
        try:
            return await coro
        finally:
            await dispose_engine()

    return asyncio.run(_runner())
