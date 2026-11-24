"""Task-local dispatch evidence, independent of vendor responses (ADR-0031)."""

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(slots=True)
class CallTracker:
    """Shared with child tasks draining a stream; never shared across answers."""

    dispatched: bool = False
    token: Token["CallTracker | None"] | None = None

    def finish(self) -> None:
        """Restore the caller's context in the task that started tracking."""
        if self.token is not None:
            _active_call.reset(self.token)
            self.token = None


_active_call: ContextVar[CallTracker | None] = ContextVar("llm_call", default=None)


def begin_call() -> CallTracker:
    """Start tracking immediately around a gateway invocation."""
    tracker = CallTracker()
    tracker.token = _active_call.set(tracker)
    return tracker


def mark_dispatched() -> None:
    """Called only after local preparation, immediately before provider I/O.

    Dispatch means the request may have reached the provider, not that billing
    is certain. A transport error before acknowledgement is still unmeasured.
    """
    tracker = _active_call.get()
    if tracker is not None:
        tracker.dispatched = True
