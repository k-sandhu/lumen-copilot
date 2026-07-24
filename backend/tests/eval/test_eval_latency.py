"""Offline unit tests for the chat-latency measurement helpers (#486 parent).

The LIVE harness (``test_chat_latency_live.py``) feeds its captured WS stream
through :mod:`tests.eval.latency`; these tests prove that measurement math on
hand-built envelope streams so the harness's own correctness is pinned offline
(no key, no stack) — the same "prove the harness, don't assume it" discipline the
rest of ``tests/eval`` follows. They run in the ordinary offline suite.
"""

from __future__ import annotations

import pytest

from tests.eval.latency import (
    StreamTiming,
    TimedEnvelope,
    fold_answer_text,
    is_answer_delta,
    measure_client_stream,
    measure_stream,
    percentile,
    summarise_ttft,
)


def _start(seq: int, ts: str) -> dict[str, object]:
    return {"type": "start", "streamId": "s", "seq": seq, "ts": ts, "data": {"model": "m"}}


def _delta(seq: int, ts: str, text: str) -> dict[str, object]:
    return {"type": "delta", "streamId": "s", "seq": seq, "ts": ts, "data": {"text": text}}


def _event(seq: int, ts: str, name: str, data: object | None = None) -> dict[str, object]:
    env: dict[str, object] = {"type": "event", "streamId": "s", "seq": seq, "ts": ts, "name": name}
    if data is not None:
        env["data"] = data
    return env


def _done(seq: int, ts: str) -> dict[str, object]:
    return {"type": "done", "streamId": "s", "seq": seq, "ts": ts, "data": {"finishReason": "stop"}}


def _error(seq: int, ts: str, *, code: str, status: int) -> dict[str, object]:
    return {
        "type": "error",
        "streamId": "s",
        "seq": seq,
        "ts": ts,
        "problem": {"title": "t", "status": status, "code": code},
    }


# --- is_answer_delta / narration boundary (AC-6) ----------------------------


def test_is_answer_delta_accepts_only_nonempty_text_deltas() -> None:
    assert is_answer_delta(_delta(1, "2026-07-23T00:00:00+00:00", "hi")) is True
    # empty text is not an answer token
    assert is_answer_delta(_delta(1, "2026-07-23T00:00:00+00:00", "")) is False
    # narration rides event:narration, never delta — must be excluded
    assert (
        is_answer_delta(_event(1, "2026-07-23T00:00:00+00:00", "narration", {"text": "x"})) is False
    )
    assert is_answer_delta(_start(0, "2026-07-23T00:00:00+00:00")) is False


# --- TTFT + total from server ts (AC-1) -------------------------------------


def test_measure_stream_ttft_is_start_to_first_answer_delta() -> None:
    envs = [
        _start(0, "2026-07-23T00:00:00.000000+00:00"),
        _event(
            1,
            "2026-07-23T00:00:00.200000+00:00",
            "tool_call",
            {"callId": "c", "tool": "search_text", "args": {}},
        ),
        _event(
            2,
            "2026-07-23T00:00:01.000000+00:00",
            "tool_result",
            {"callId": "c", "tool": "search_text", "hitCount": 1},
        ),
        _delta(3, "2026-07-23T00:00:01.500000+00:00", "Based on your "),
        _delta(4, "2026-07-23T00:00:01.600000+00:00", "documents..."),
        _done(5, "2026-07-23T00:00:02.000000+00:00"),
    ]
    t = measure_stream(envs)
    assert t.ttft_seconds == pytest.approx(1.5, abs=1e-6)  # start → first delta
    assert t.total_seconds == pytest.approx(2.0, abs=1e-6)  # start → done
    assert t.terminal_type == "done"
    assert t.answer_text == "Based on your documents..."
    assert t.answer_delta_count == 2
    assert t.retract_count == 0


def test_measure_stream_ignores_narration_and_empty_deltas_for_ttft() -> None:
    # A tool-calling turn narrates first (event:narration), then the ANSWER turn
    # streams — TTFT must skip the narration and land on the first answer delta.
    envs = [
        _start(0, "2026-07-23T00:00:00.000000+00:00"),
        _event(
            1, "2026-07-23T00:00:00.100000+00:00", "narration", {"text": "Let me search", "turn": 1}
        ),
        _delta(2, "2026-07-23T00:00:00.900000+00:00", "The answer"),
        _done(3, "2026-07-23T00:00:01.000000+00:00"),
    ]
    t = measure_stream(envs)
    assert t.ttft_seconds == pytest.approx(0.9, abs=1e-6)


# --- speculative retraction folding (#488 / AC-6) ---------------------------


def test_fold_answer_text_resets_on_retract() -> None:
    envs = [
        _start(0, "2026-07-23T00:00:00.000000+00:00"),
        _delta(1, "2026-07-23T00:00:00.500000+00:00", "speculative "),
        _delta(2, "2026-07-23T00:00:00.600000+00:00", "text"),
        _event(3, "2026-07-23T00:00:00.700000+00:00", "answer_retract"),
        _delta(4, "2026-07-23T00:00:01.000000+00:00", "real answer"),
        _done(5, "2026-07-23T00:00:01.200000+00:00"),
    ]
    assert fold_answer_text(envs) == "real answer"
    t = measure_stream(envs)
    assert t.answer_text == "real answer"
    assert t.retract_count == 1
    # TTFT is time-to-first-token-on-the-wire (what the user saw appear); the live
    # AC-1 scenario is single-tool so retract_count is asserted 0 there.
    assert t.ttft_seconds == pytest.approx(0.5, abs=1e-6)


# --- typed terminal error (AC-7) --------------------------------------------


def test_measure_stream_classifies_typed_error_terminal() -> None:
    envs = [
        _start(0, "2026-07-23T00:00:00.000000+00:00"),
        _error(1, "2026-07-23T00:00:24.000000+00:00", code="dependency_unavailable", status=503),
    ]
    t = measure_stream(envs)
    assert t.terminal_type == "error"
    assert t.problem_code == "dependency_unavailable"
    assert t.problem_status == 503
    assert t.total_seconds == pytest.approx(24.0, abs=1e-6)
    assert t.ttft_seconds is None  # no answer token ever arrived


# --- robustness: missing pieces --------------------------------------------


def test_measure_stream_handles_missing_start_and_terminal() -> None:
    t = measure_stream([_delta(1, "2026-07-23T00:00:00+00:00", "x")])
    assert t.ttft_seconds is None  # no start to measure from
    assert t.total_seconds is None
    assert t.terminal_type is None


def test_measure_stream_tolerates_unparseable_ts() -> None:
    envs = [_start(0, "not-a-timestamp"), _delta(1, "also-bad", "x"), _done(2, "nope")]
    t = measure_stream(envs)
    assert t.ttft_seconds is None
    assert t.total_seconds is None
    assert t.terminal_type == "done"  # structure still classified


# --- client-perspective timing (FE-1: perf_counter at receipt, not server ts) ---


def _te(recv: float, envelope: dict[str, object]) -> TimedEnvelope:
    """A received envelope stamped with a monotonic client-receipt time."""
    return TimedEnvelope(recv=recv, envelope=envelope)


def test_measure_client_stream_ttft_and_total_from_receipt_times() -> None:
    # recv times are perf_counter readings the consumer took as each envelope
    # arrived off the backplane; the ts strings are deliberately different so a
    # regression to server-ts measurement would be caught.
    events = [
        _te(100.00, _start(0, "2026-07-23T00:00:00.000000+00:00")),
        _te(
            100.10, _event(1, "2026-07-23T00:00:00.050000+00:00", "narration", {"text": "hunting"})
        ),
        _te(100.90, _delta(2, "2026-07-23T00:00:00.400000+00:00", "Based on ")),
        _te(101.00, _delta(3, "2026-07-23T00:00:00.450000+00:00", "your docs")),
        _te(101.50, _done(4, "2026-07-23T00:00:00.500000+00:00")),
    ]
    t = measure_client_stream(events)
    assert t.ttft_seconds == pytest.approx(0.9, abs=1e-9)  # start recv -> first answer-delta recv
    assert t.total_seconds == pytest.approx(1.5, abs=1e-9)  # start recv -> terminal recv
    assert t.terminal_type == "done"
    assert t.answer_text == "Based on your docs"
    assert t.answer_delta_count == 2
    assert t.retract_count == 0


def test_measure_client_stream_measures_receipt_not_server_ts() -> None:
    # The FE-1 crux: server ts says first token was 0.4s after start; the client
    # actually received it 0.9s after start (publish + backplane + relay). AC-1 is
    # the client number, so measure_client_stream must report 0.9, not 0.4.
    events = [
        _te(100.00, _start(0, "2026-07-23T00:00:00.000000+00:00")),
        _te(100.90, _delta(1, "2026-07-23T00:00:00.400000+00:00", "answer")),
        _te(101.30, _done(2, "2026-07-23T00:00:00.700000+00:00")),
    ]
    t = measure_client_stream(events)
    assert t.ttft_seconds == pytest.approx(0.9, abs=1e-9)
    assert t.total_seconds == pytest.approx(1.3, abs=1e-9)


def test_measure_client_stream_ignores_narration_for_ttft() -> None:
    events = [
        _te(50.0, _start(0, "2026-07-23T00:00:00.000000+00:00")),
        _te(
            50.2,
            _event(1, "2026-07-23T00:00:00.100000+00:00", "narration", {"text": "x", "turn": 1}),
        ),
        _te(50.8, _delta(2, "2026-07-23T00:00:00.900000+00:00", "The answer")),
        _te(51.0, _done(3, "2026-07-23T00:00:01.000000+00:00")),
    ]
    assert measure_client_stream(events).ttft_seconds == pytest.approx(0.8, abs=1e-9)


def test_measure_client_stream_classifies_typed_error_terminal() -> None:
    events = [
        _te(200.0, _start(0, "2026-07-23T00:00:00.000000+00:00")),
        _te(
            225.0,
            _error(
                1, "2026-07-23T00:00:24.000000+00:00", code="dependency_unavailable", status=503
            ),
        ),
    ]
    t = measure_client_stream(events)
    assert t.terminal_type == "error"
    assert t.problem_code == "dependency_unavailable"
    assert t.problem_status == 503
    assert t.total_seconds == pytest.approx(25.0, abs=1e-9)  # client-side start -> error
    assert t.ttft_seconds is None  # no answer token ever arrived


def test_measure_client_stream_folds_retract_and_reports_first_wire_ttft() -> None:
    events = [
        _te(10.0, _start(0, "2026-07-23T00:00:00.000000+00:00")),
        _te(10.5, _delta(1, "2026-07-23T00:00:00.500000+00:00", "speculative ")),
        _te(10.6, _delta(2, "2026-07-23T00:00:00.600000+00:00", "text")),
        _te(10.7, _event(3, "2026-07-23T00:00:00.700000+00:00", "answer_retract")),
        _te(11.0, _delta(4, "2026-07-23T00:00:01.000000+00:00", "real answer")),
        _te(11.2, _done(5, "2026-07-23T00:00:01.200000+00:00")),
    ]
    t = measure_client_stream(events)
    assert t.answer_text == "real answer"
    assert t.retract_count == 1
    assert t.ttft_seconds == pytest.approx(0.5, abs=1e-9)  # first token on the wire


def test_measure_client_stream_handles_missing_start_and_terminal() -> None:
    t = measure_client_stream([_te(1.0, _delta(1, "2026-07-23T00:00:00+00:00", "x"))])
    assert t.ttft_seconds is None  # no start receipt to measure from
    assert t.total_seconds is None
    assert t.terminal_type is None
    assert t.answer_delta_count == 1


# --- percentile math --------------------------------------------------------


def test_percentile_p50_is_median_of_odd_sample() -> None:
    assert percentile([3.0, 1.0, 2.0], 0.5) == pytest.approx(2.0)


def test_percentile_interpolates() -> None:
    assert percentile([0.0, 10.0], 0.5) == pytest.approx(5.0)
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.95) == pytest.approx(3.85)


def test_percentile_single_value_and_bounds() -> None:
    assert percentile([7.0], 0.5) == 7.0
    assert percentile([1.0, 2.0], 0.0) == 1.0
    assert percentile([1.0, 2.0], 1.0) == 2.0


def test_percentile_rejects_empty_and_out_of_range() -> None:
    with pytest.raises(ValueError):
        percentile([], 0.5)
    with pytest.raises(ValueError):
        percentile([1.0], 1.5)


# --- aggregation ------------------------------------------------------------


def _timing(ttft: float, total: float) -> StreamTiming:
    return StreamTiming(
        ttft_seconds=ttft,
        total_seconds=total,
        terminal_type="done",
        problem_code=None,
        problem_status=None,
        answer_text="a",
        answer_delta_count=1,
        retract_count=0,
    )


def test_summarise_ttft_aggregates_and_formats() -> None:
    report = summarise_ttft([_timing(1.0, 3.0), _timing(1.5, 3.5), _timing(1.2, 3.2)])
    assert report.samples == 3
    assert report.ttft_p50 == pytest.approx(1.2)
    assert report.ttft_max == pytest.approx(1.5)
    body = report.format(budget_seconds=2.0, backplane="redis")
    assert "PASS" in body
    assert "redis" in body
    # A p50 above budget must read FAIL, so the harness cannot "look faster".
    slow = summarise_ttft([_timing(2.5, 4.0)])
    assert "FAIL" in slow.format(budget_seconds=2.0, backplane="redis")


def test_summarise_ttft_rejects_unmeasurable_sample() -> None:
    unmeasurable = StreamTiming(
        ttft_seconds=None,
        total_seconds=None,
        terminal_type=None,
        problem_code=None,
        problem_status=None,
        answer_text="",
        answer_delta_count=0,
        retract_count=0,
    )
    with pytest.raises(ValueError):
        summarise_ttft([unmeasurable])
