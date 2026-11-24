"""One execution deadline shared by acquisition, preparation and collection."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from contextvars import ContextVar

current_deadline: ContextVar[ExecutionDeadline | None] = ContextVar(
    "execution_deadline", default=None
)


class ExecutionDeadline:
    def __init__(self, seconds: float | None) -> None:
        self.ends_at = time.monotonic() + seconds if seconds is not None else None
        self.expired = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    def remaining(self) -> float | None:
        if self.expired.is_set():
            raise TimeoutError("execution deadline expired")
        if self.ends_at is None:
            return None
        remaining = self.ends_at - time.monotonic()
        if remaining <= 0:
            self.expire()
            raise TimeoutError("execution deadline expired")
        return remaining

    def expire(self) -> None:
        with self._lock:
            self.expired.set()
            callbacks, self._callbacks = self._callbacks, []
        # Close active I/O and kill runner-side work before container teardown.
        for callback in reversed(callbacks):
            with contextlib.suppress(Exception):
                callback()

    @contextlib.contextmanager
    def watch(self, callback: Callable[[], None]) -> Iterator[None]:
        with self._lock:
            expired = self.expired.is_set()
            if not expired:
                self._callbacks.append(callback)
        if expired:
            callback()
        try:
            self.remaining()
            yield
        finally:
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)


def check_deadline() -> float | None:
    deadline = current_deadline.get()
    return deadline.remaining() if deadline else None
