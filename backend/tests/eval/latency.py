"""Chat-latency measurement helpers — pure envelope→timing math (#486 parent).

The parent tracking issue's *instrumentation caveat*: AC-1 (time-to-first-ANSWER
token < 2s p50) and AC-7 (a typed terminal error within the interactive budget
against an unreachable provider) cannot be verified without a client-perspective
timing harness, because the repo has no metrics and no tracing. These helpers are
the **I/O-free core** of that harness: they turn a captured WS envelope stream
(exactly what a WS client would receive off ``realtime/`` — the same shapes the
relay forwards) into the two numbers AC-1 measures and classify the terminal for
AC-7.

They are deliberately pure so the harness's own measurement math is unit-tested
offline (``test_eval_latency.py``), while the LIVE harness
(``test_chat_latency_live.py``) wires the real runtime / gateway / backplane and
feeds its captured stream through *exactly these functions* — so what the live
run asserts is what the offline test pins (the "prove the harness, don't assume
it" discipline the rest of ``tests/eval`` already follows).

**Timing source — the server ``ts``.** Every envelope carries a server-set ``ts``
(ISO-8601; "Server send time" per ``contracts/websocket-envelopes.schema.json``).
Measuring from envelope ``ts`` deltas — ``start.ts`` → first-answer-``delta``.ts —
is the client-perspective wall-clock from the ``start`` envelope to the first
answer token being **sent**, and is immune to the harness's own subscribe race
(with the bounded replay, the producer may well finish before the consumer drains
it, so consumer arrival times are meaningless; the embedded ``ts`` is not). This
is precisely AC-1's "from the client's ``start`` envelope to its first ``delta``
carrying answer text".

**Answer text, not narration (AC-6).** Only ``type:"delta"`` envelopes carry
answer text; a tool-calling turn's narration rides ``event:"narration"`` and is
never counted here. Speculative streaming (#488) may stream answer ``delta``\\ s
before a turn is proven tool-free and then retract the whole block with one
``event:answer_retract``; ``fold_answer_text`` applies those resets so the folded
answer equals what the client renders (and what the runtime persists).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

_TERMINAL_TYPES = frozenset({"done", "error"})


def _envelope_ts(envelope: Mapping[str, object]) -> float | None:
    """Parse an envelope's server ``ts`` into epoch seconds, or ``None``.

    Robust to a missing / non-string / unparseable ``ts`` (returns ``None`` rather
    than raising) so a stream with a stray malformed envelope still measures.
    """
    raw = envelope.get("ts")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def is_answer_delta(envelope: Mapping[str, object]) -> bool:
    """True for a ``delta`` envelope carrying non-empty answer text.

    Narration (``event:narration``), empty deltas, and every non-``delta`` event
    are excluded — this is the AC-6 answer-vs-narration boundary in one predicate.
    """
    if envelope.get("type") != "delta":
        return False
    data = envelope.get("data")
    if not isinstance(data, Mapping):
        return False
    text = data.get("text")
    return isinstance(text, str) and text != ""


def _is_answer_retract(envelope: Mapping[str, object]) -> bool:
    return envelope.get("type") == "event" and envelope.get("name") == "answer_retract"


def fold_answer_text(envelopes: Sequence[Mapping[str, object]]) -> str:
    """Concatenate answer ``delta`` text, resetting on each ``answer_retract``.

    Mirrors the frontend reducer (``streamReducer.ts`` ``answer_retract`` clears
    ``text``) and the runtime's persisted-answer folding: the returned string is
    what the client ultimately renders and what the assistant message row stores,
    so an equality check against the persisted answer proves the #148/#488
    invariant (AC-6). A retract discards *only* the speculative deltas that
    preceded it in the current turn.
    """
    parts: list[str] = []
    for env in envelopes:
        if _is_answer_retract(env):
            parts.clear()
            continue
        if is_answer_delta(env):
            data = env.get("data")
            assert isinstance(data, Mapping)  # narrowed by is_answer_delta
            parts.append(str(data.get("text", "")))
    return "".join(parts)


@dataclass(frozen=True, slots=True)
class StreamTiming:
    """The client-perspective timing of one captured answer stream.

    All times are seconds relative to the ``start`` envelope's ``ts``. ``None``
    where a stream lacked the envelope needed to compute it (no ``start``, no
    answer delta, no terminal, or an unparseable ``ts``) — the caller decides
    whether that is a skip or a failure.
    """

    ttft_seconds: float | None
    """``start.ts`` → first answer-``delta``.ts (AC-1). The visible time-to-first
    -token; in a single-tool grounded answer there is no retract, so it is also
    the time-to-first-ANSWER-token exactly."""
    total_seconds: float | None
    """``start.ts`` → terminal.ts — the total time the answer took to settle."""
    terminal_type: str | None
    """``"done"`` | ``"error"`` | ``None`` (the stream never terminated)."""
    problem_code: str | None
    """For an ``error`` terminal: the typed Problem ``code`` (AC-7 "typed")."""
    problem_status: int | None
    """For an ``error`` terminal: the Problem ``status`` (e.g. 503)."""
    answer_text: str
    """The folded answer (retracts applied) — the client-rendered / persisted text."""
    answer_delta_count: int
    """How many answer ``delta`` envelopes carried text (envelope-count, for AC-2/
    coalescing observation — one publish per delta on the wire)."""
    retract_count: int
    """How many ``event:answer_retract`` envelopes fired (0 in the happy single-
    tool path — the AC-1 scenario asserts this so TTFT is unambiguous)."""


def measure_stream(envelopes: Sequence[Mapping[str, object]]) -> StreamTiming:
    """Reduce a captured envelope stream to its client-perspective timing.

    Uses the embedded server ``ts`` (see the module docstring) so the result is
    independent of when the harness happened to drain the replay.
    """
    start_ts: float | None = None
    first_answer_ts: float | None = None
    terminal_ts: float | None = None
    terminal_type: str | None = None
    problem_code: str | None = None
    problem_status: int | None = None
    answer_delta_count = 0
    retract_count = 0

    for env in envelopes:
        etype = env.get("type")
        if etype == "start" and start_ts is None:
            start_ts = _envelope_ts(env)
            continue
        if _is_answer_retract(env):
            retract_count += 1
            continue
        if is_answer_delta(env):
            answer_delta_count += 1
            if first_answer_ts is None:
                first_answer_ts = _envelope_ts(env)
            continue
        if etype in _TERMINAL_TYPES:
            # The LAST terminal wins (there is exactly one in a well-formed stream;
            # scanning to the end tolerates a trailing post-terminal event).
            terminal_ts = _envelope_ts(env)
            terminal_type = str(etype)
            problem_code = None
            problem_status = None
            if etype == "error":
                problem = env.get("problem")
                if isinstance(problem, Mapping):
                    code = problem.get("code")
                    status = problem.get("status")
                    problem_code = code if isinstance(code, str) else None
                    problem_status = status if isinstance(status, int) else None

    def _delta(a: float | None, b: float | None) -> float | None:
        return None if a is None or b is None else a - b

    return StreamTiming(
        ttft_seconds=_delta(first_answer_ts, start_ts),
        total_seconds=_delta(terminal_ts, start_ts),
        terminal_type=terminal_type,
        problem_code=problem_code,
        problem_status=problem_status,
        answer_text=fold_answer_text(envelopes),
        answer_delta_count=answer_delta_count,
        retract_count=retract_count,
    )


def percentile(values: Sequence[float], p: float) -> float:
    """The ``p``-quantile (``p`` in ``[0, 1]``) by linear interpolation.

    Matches the common "type 7" / NumPy-default definition so p50 of an
    odd-length sample is the median and p95 interpolates between ranks. Raises on
    an empty sample (a percentile of nothing is undefined — the caller must have
    at least one measurement).
    """
    if not values:
        raise ValueError("percentile of an empty sample is undefined")
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"p must be in [0, 1], got {p}")
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    rank = p * (len(xs) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (rank - lo)


@dataclass(frozen=True, slots=True)
class LatencyReport:
    """Aggregate TTFT / total across N sampled answers (AC-1)."""

    samples: int
    ttft_p50: float
    ttft_p95: float
    ttft_max: float
    total_p50: float

    def format(self, *, budget_seconds: float, backplane: str) -> str:
        """A human-readable one-block summary for the harness's ``-s`` output."""
        verdict = "PASS" if self.ttft_p50 < budget_seconds else "FAIL"
        p50 = f"{self.ttft_p50:.3f}s  (budget < {budget_seconds:.3f}s -> {verdict})"
        return (
            "chat-latency AC-1 (time-to-first-answer-token)\n"
            f"  backplane        : {backplane}\n"
            f"  samples          : {self.samples}\n"
            f"  TTFT p50         : {p50}\n"
            f"  TTFT p95         : {self.ttft_p95:.3f}s\n"
            f"  TTFT max         : {self.ttft_max:.3f}s\n"
            f"  total answer p50 : {self.total_p50:.3f}s"
        )


def summarise_ttft(timings: Sequence[StreamTiming]) -> LatencyReport:
    """Aggregate a batch of AC-1 samples into a :class:`LatencyReport`.

    Requires every sample to carry a TTFT and a total (a sample that never
    produced an answer delta or never terminated is a measurement failure, not a
    zero — the live harness asserts this before calling in).
    """
    if not timings:
        raise ValueError("no timings to summarise")
    ttfts: list[float] = []
    totals: list[float] = []
    for t in timings:
        if t.ttft_seconds is None or t.total_seconds is None:
            raise ValueError("a sample is missing TTFT or total — not measurable")
        ttfts.append(t.ttft_seconds)
        totals.append(t.total_seconds)
    return LatencyReport(
        samples=len(timings),
        ttft_p50=percentile(ttfts, 0.5),
        ttft_p95=percentile(ttfts, 0.95),
        ttft_max=max(ttfts),
        total_p50=percentile(totals, 0.5),
    )


__all__ = [
    "LatencyReport",
    "StreamTiming",
    "fold_answer_text",
    "is_answer_delta",
    "measure_stream",
    "percentile",
    "summarise_ttft",
]
