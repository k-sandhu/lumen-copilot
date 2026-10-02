"""Track loops instead of scanning the Python heap after every test (#94)."""

from __future__ import annotations

import asyncio
import threading

import pytest


class LoopTracker:
    def __init__(self) -> None:
        # Strong references keep cyclic orphans alive until we close their
        # self-pipe sockets, before GC can warn in an unrelated test.
        self.loops: set[asyncio.AbstractEventLoop] = set()
        self._lock = threading.Lock()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = asyncio.BaseEventLoop.__init__

        def tracked_init(loop: asyncio.BaseEventLoop) -> None:
            original(loop)
            with self._lock:
                self.loops.add(loop)

        # Covers policies, direct SelectorEventLoop constructors, asyncio.run,
        # and pytest-asyncio's replacement loops, including loops in threads.
        monkeypatch.setattr(asyncio.BaseEventLoop, "__init__", tracked_init)

    def close_idle(self) -> None:
        with self._lock:
            for loop in tuple(self.loops):
                if loop.is_running():
                    continue
                if not loop.is_closed():
                    loop.close()
                self.loops.remove(loop)
