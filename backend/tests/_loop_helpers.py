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
        # BaseEventLoop.__init__ returns before concrete loops create their
        # self-pipes. Publish only after the entire standard constructor returns.
        self._track_constructor(asyncio.SelectorEventLoop, monkeypatch)
        proactor = getattr(asyncio, "ProactorEventLoop", None)
        if proactor is not None:
            self._track_constructor(proactor, monkeypatch)

    def _track_constructor(
        self, loop_type: type[asyncio.BaseEventLoop], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = loop_type.__init__

        def tracked_init(loop: asyncio.BaseEventLoop, *args: object, **kwargs: object) -> None:
            original(loop, *args, **kwargs)
            with self._lock:
                self.loops.add(loop)

        # Covers policies, direct SelectorEventLoop constructors, asyncio.run,
        # and pytest-asyncio's replacement loops, including loops in threads.
        monkeypatch.setattr(loop_type, "__init__", tracked_init)

    def close_idle(self) -> None:
        with self._lock:
            for loop in tuple(self.loops):
                if loop.is_running():
                    continue
                if not loop.is_closed():
                    try:
                        loop.close()
                    except RuntimeError as exc:
                        # A thread can start the loop after the idle snapshot.
                        # It may also stop before close returns: use the close
                        # refusal, not another racy is_running snapshot.
                        if str(exc) != "Cannot close a running event loop":
                            raise
                        continue
                self.loops.remove(loop)
