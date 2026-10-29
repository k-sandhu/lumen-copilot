"""Stream-keyed registry of in-flight answer producers (#574).

Replaces the unkeyed ``answer_tasks`` set: every producer task is registered
under its ``stream_id`` together with its runtime, so the cancel endpoint can
address exactly one answer. Cancellation is two-step by design: the runtime
is first marked user-cancelled (so its ``CancelledError`` arm knows this is a
Stop, not a shutdown), then the task is cancelled (which also cancels any
in-flight tool await inside it). Completed tasks and their runtime references
are pruned immediately; bounded backplane replay owns the settled outcome
and idempotency across replicas.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol


class CancellableRuntime(Protocol):
    """The slice of ``ChatRuntime`` the registry needs."""

    def request_cancel(self) -> bool | None:
        """Mark the in-flight answer as user-cancelled."""
        ...


@dataclass(slots=True)
class _Entry:
    task: asyncio.Task[object]
    runtime: CancellableRuntime | None


class StreamRegistry:
    """``stream_id -> (producer task, runtime)`` for this process."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def register(
        self,
        stream_id: str,
        *,
        task: asyncio.Task[object],
        runtime: CancellableRuntime | None,
    ) -> None:
        self._entries[stream_id] = _Entry(task=task, runtime=runtime)
        task.add_done_callback(lambda _task: self._entries.pop(stream_id, None))

    def known(self, stream_id: str) -> bool:
        """True iff this process currently tracks ``stream_id``."""
        return stream_id in self._entries

    def is_active(self, stream_id: str) -> bool:
        entry = self._entries.get(stream_id)
        return entry is not None and not entry.task.done()

    def cancel(self, stream_id: str) -> bool:
        """Cancel the producer for ``stream_id``; True iff one was active.

        Idempotent: an unknown or already-settled stream returns False and
        nothing is signalled twice (the runtime emits exactly one terminal
        and one audit event regardless of repeat cancels).
        """
        entry = self._entries.get(stream_id)
        if entry is None or entry.task.done() or entry.task.cancelling():
            return False
        if entry.runtime is not None:
            accepted = entry.runtime.request_cancel()
            if accepted is False:
                return False
            if accepted is None:
                entry.task.cancel()
        else:
            entry.task.cancel()
        return True

    def tasks(self) -> list[asyncio.Task[object]]:
        """All registered tasks (the shutdown drain set)."""
        return [entry.task for entry in self._entries.values()]


__all__ = ["CancellableRuntime", "StreamRegistry"]
