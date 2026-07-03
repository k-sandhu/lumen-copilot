"""Persisting + fan-out sinks behind the chat runtime's ``Backplane`` seam (ADR-0015 §3).

The headless-run runtime is the **smallest correct refactor** of the chat runtime:
that loop already publishes every ``delta`` / tool event / citation through the
``realtime.Backplane`` Protocol (``publish(stream_id, envelope)``), and the WS
consumer relays them. A scheduled/manual run has **no socket attached**, so it
passes one of these sinks instead of the ``RedisBackplane``:

* :class:`RunTranscriptSink` — captures each published envelope as a durable
  ``run_steps`` row (``seq``/``kind``/``payload``) and folds the terminal
  (``done``/``error``) into the run's ``status``/``summary``/``error``. The run
  then produces a **persisted, cited transcript with no socket present** — the core
  requirement. The agentic loop's shape (INV-3 grounding, the tool-turn budget, the
  exactly-one-terminal lifecycle) is **untouched**; only *where the envelopes go*
  changes, so a headless run cannot fabricate or over-cite any more than an
  interactive one can.
* :class:`TeeSink` — a fan-out over ``[transcript, redis]`` so a run that is being
  **watched** dual-publishes: to the transcript (always, durable) *and* to the
  ``RedisBackplane`` (so a live watcher sees deltas in real time, ADR-0015 §3).
  An unwatched run skips the Redis leg entirely (no wasted fan-out) by simply not
  wrapping the transcript in a tee.

These are ``services/`` collaborators (the runtime's, not a new boundary owner):
they only implement the ``publish`` half of the Protocol used by the producer. The
``bind_owner``/``get_owner``/``subscribe`` half is the *consumer's* concern (the WS
endpoint), and a live watcher attaches over the existing chat WS with the run id as
the ``streamId`` — so those methods here are no-ops / not reached on the producer
path (the transcript is the durable replay; Redis is the live leg via the tee).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.db.repositories import RunRepository, RunStepRepository
from app.domain.entities import RunError, RunStatus, RunStepKind
from app.realtime.backplane import Backplane, StreamOwner, is_terminal

# The WS envelope ``type`` / ``event name`` set → the durable ``RunStepKind``. A
# ``delta`` is incremental text; an ``event`` carries a ``name`` in the tool/citation
# vocabulary; an ``error`` terminal becomes an ``error`` step. A ``start``/``done``
# envelope is lifecycle framing (folded into the run status), not a transcript step,
# so it maps to ``None`` and is not persisted as a step.
_EVENT_NAME_KINDS: dict[str, RunStepKind] = {
    "tool_call": RunStepKind.TOOL_CALL,
    "tool_result": RunStepKind.TOOL_RESULT,
    "citation": RunStepKind.CITATION,
}


def _step_kind(envelope: dict[str, Any]) -> RunStepKind | None:
    """Map a WS envelope to its persisted :class:`RunStepKind`, or ``None`` to skip.

    ``start``/``done`` are lifecycle framing (the run status carries them), so they
    are not transcript steps. A ``delta`` and each named ``event`` in the tool/
    citation vocabulary is a step; an ``error`` terminal is an ``error`` step (so
    the failure is visible in the transcript, not only in ``runs.error``).
    """
    env_type = envelope.get("type")
    if env_type == "delta":
        return RunStepKind.DELTA
    if env_type == "error":
        return RunStepKind.ERROR
    if env_type == "event":
        name = envelope.get("name")
        return _EVENT_NAME_KINDS.get(name) if isinstance(name, str) else None
    return None


@dataclass
class RunTranscriptSink:
    """A :class:`Backplane` that persists a run's stream to ``run_steps`` (ADR-0015 §3).

    Captures every envelope the runtime publishes in memory (ordered, with the
    runtime's monotonic ``seq``) so the whole transcript can be written in the run
    task's own transaction via :meth:`persist`. Folding the terminal into the run's
    status is :meth:`outcome`. The runtime's ``publish`` is ``async`` and called on
    the run's session's event loop; buffering keeps the sink dependency-free (it
    holds no session of its own — the task owns the transaction boundary, so the
    steps commit atomically with the run's terminal update).

    Buffering (rather than a write per ``publish``) is deliberate: it keeps the
    sink a pure collaborator with no session/loop coupling, and the transcript is a
    bounded single answer's output (the same ``_MAX_REPLAY`` ceiling the backplanes
    cap at). Live watchers get real-time deltas via the :class:`TeeSink`'s Redis
    leg; the transcript is the durable replay drained on (re)attach.
    """

    stream_id: str
    _envelopes: list[dict[str, Any]] = field(default_factory=list)

    async def publish(self, stream_id: str, envelope: dict[str, Any]) -> None:
        """Capture one published envelope (the producer-side Protocol method)."""
        self._envelopes.append(envelope)

    async def bind_owner(self, stream_id: str, owner: StreamOwner) -> None:
        """No-op: the transcript is durable, so the producer needs no owner binding.

        Owner/tenant binding for a *live* watcher lives on the Redis leg of the
        :class:`TeeSink` (reusing the backplane's ``StreamOwner``); the transcript
        itself is tenant-scoped in the DB (RLS), so this producer-side sink stores
        no separate binding.
        """

    async def get_owner(self, stream_id: str) -> StreamOwner | None:  # pragma: no cover
        return None

    def subscribe(self, stream_id: str) -> AsyncGenerator[dict[str, Any], None]:  # pragma: no cover
        raise NotImplementedError("RunTranscriptSink is producer-only; watch via the chat WS.")

    # --- captured-stream projections ---------------------------------------

    @property
    def envelopes(self) -> list[dict[str, Any]]:
        """The captured envelopes in publish order (for assertions / dual-publish)."""
        return list(self._envelopes)

    def outcome(self) -> _RunOutcome:
        """Fold the captured terminal envelope into a run outcome (status + summary/error).

        A ``done`` terminal ⇒ ``succeeded`` with a one-line ``summary`` distilled
        from the streamed deltas + citation count. An ``error`` terminal ⇒ ``failed``
        with a typed :class:`RunError` built from the problem envelope (a stable
        ``code`` + human ``message`` — never a raw vendor string). No terminal seen
        (should not happen: the runtime always emits exactly one) ⇒ ``failed`` with
        a ``no_terminal`` reason, so a run never ends in an unknown state.
        """
        terminal = next(
            (e for e in reversed(self._envelopes) if is_terminal(e)),
            None,
        )
        if terminal is None:  # pragma: no cover — the runtime guarantees one terminal
            return _RunOutcome(
                status=RunStatus.FAILED,
                summary=None,
                error=RunError(code="no_terminal", message="The run produced no terminal event."),
            )
        if terminal.get("type") == "error":
            problem = terminal.get("problem")
            code = "run_failed"
            message = "The run failed."
            if isinstance(problem, dict):
                raw_code = problem.get("code")
                raw_detail = problem.get("detail") or problem.get("title")
                if isinstance(raw_code, str) and raw_code:
                    code = raw_code
                if isinstance(raw_detail, str) and raw_detail:
                    message = raw_detail
            return _RunOutcome(
                status=RunStatus.FAILED,
                summary=None,
                error=RunError(code=code, message=message),
            )
        return _RunOutcome(status=RunStatus.SUCCEEDED, summary=self._summarize(), error=None)

    def message_id(self) -> UUID | None:
        """The assistant message id the run produced (from the ``start`` envelope).

        The runtime mints the message id up front and rides it on ``start`` (and the
        ``done`` summary), so the run links to its grounded, cited answer without a
        second lookup. ``None`` if no ``start`` carried one (defensive).
        """
        for env in self._envelopes:
            if env.get("type") == "start":
                data = env.get("data")
                if isinstance(data, dict):
                    raw = data.get("messageId")
                    if isinstance(raw, str):
                        try:
                            return UUID(raw)
                        except ValueError:  # pragma: no cover
                            return None
        return None

    def _summarize(self) -> str:
        """A short inbox digest line from the streamed answer text (ADR-0015 §6).

        Concatenates the ``delta`` text and trims to a single leading line/clause so
        the ``/runs`` inbox shows a meaningful preview without loading the full
        transcript. Empty (a no-source answer) yields a stable placeholder.
        """
        text = "".join(
            str(e.get("data", {}).get("text", ""))
            for e in self._envelopes
            if e.get("type") == "delta" and isinstance(e.get("data"), dict)
        ).strip()
        if not text:
            return "The run produced no answer content."
        first_line = text.splitlines()[0].strip()
        return first_line[:200] if len(first_line) > 200 else first_line

    async def persist(
        self, *, runs: RunRepository, steps: RunStepRepository, run_id: UUID
    ) -> None:
        """Write the captured transcript to ``run_steps`` (append-only), tenant-scoped.

        Called by the run task inside its own transaction so the transcript commits
        atomically with the run's terminal update. Each envelope maps to at most one
        step (``start``/``done`` framing is skipped — the run status carries them);
        the runtime's monotonic ``seq`` is preserved so ``(run_id, seq)`` stays the
        transcript's stable order. Envelopes with no ``seq`` (defensive) fall back to
        the loop index.
        """
        for index, env in enumerate(self._envelopes):
            kind = _step_kind(env)
            if kind is None:
                continue
            raw_seq = env.get("seq")
            seq = raw_seq if isinstance(raw_seq, int) else index
            payload = env.get("data")
            if env.get("type") == "error":
                payload = env.get("problem")
            await steps.add(
                run_id=run_id,
                seq=seq,
                kind=kind,
                payload=payload if isinstance(payload, dict) else {},
            )


@dataclass(frozen=True, slots=True)
class _RunOutcome:
    """The terminal outcome folded from a run's captured stream (status + summary/error)."""

    status: RunStatus
    summary: str | None
    error: RunError | None


class TeeSink:
    """Fan-out :class:`Backplane` over several sinks — the dual-publish for a watched run.

    Publishes each envelope to every wrapped sink in order, so a run being watched
    goes to both the durable :class:`RunTranscriptSink` **and** the live
    ``RedisBackplane`` (ADR-0015 §3). The loop never knows whether anyone is
    watching — an unwatched run is simply not wrapped in a tee. ``bind_owner`` is
    forwarded so the Redis leg gets the run-stream owner binding (INV-1/INV-2 on the
    *watch* path); ``get_owner``/``subscribe`` delegate to the first sink (the
    consumer never uses this producer-side tee).
    """

    def __init__(self, sinks: list[Backplane]) -> None:
        if not sinks:  # pragma: no cover — a tee always wraps at least one sink
            raise ValueError("TeeSink requires at least one sink")
        self._sinks = sinks

    async def publish(self, stream_id: str, envelope: dict[str, Any]) -> None:
        for sink in self._sinks:
            await sink.publish(stream_id, envelope)

    async def bind_owner(self, stream_id: str, owner: StreamOwner) -> None:
        for sink in self._sinks:
            await sink.bind_owner(stream_id, owner)

    async def get_owner(self, stream_id: str) -> StreamOwner | None:  # pragma: no cover
        return await self._sinks[0].get_owner(stream_id)

    def subscribe(self, stream_id: str) -> AsyncGenerator[dict[str, Any], None]:  # pragma: no cover
        return self._sinks[0].subscribe(stream_id)


__all__ = ["RunTranscriptSink", "TeeSink"]
