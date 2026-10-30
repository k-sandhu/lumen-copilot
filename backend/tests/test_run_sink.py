"""Run sink tests — the persisting + fan-out sinks behind the runtime seam (ADR-0015 §3, #235).

Unit-level, no DB: the :class:`RunTranscriptSink` maps published WS envelopes to
the right ``RunStepKind`` (skipping ``start``/``done`` framing), folds the terminal
into a run outcome (``done`` → succeeded + summary; ``error`` → failed + typed
error), and extracts the message id; the :class:`TeeSink` fans out to every wrapped
sink (the dual-publish for a watched run — ADR-0015 §3). These fix the pure mapping
so the DB-backed transcript persistence (``test_runs_service``) can trust it.
"""

from __future__ import annotations

from typing import Any

from app.domain.entities import RunStatus, RunStepKind
from app.realtime import envelopes
from app.services.run_sink import RunTranscriptSink, TeeSink


async def _publish_all(sink: object, stream_id: str, envs: list[dict[str, Any]]) -> None:
    for env in envs:
        await sink.publish(stream_id, env)  # type: ignore[attr-defined]


def _stream(message_id: str) -> list[dict[str, Any]]:
    """A representative successful run stream (start → tool → cite → delta → done)."""
    return [
        envelopes.start("s1", 0, data={"messageId": message_id, "model": "m"}),
        envelopes.event("s1", 1, name="tool_call", data={"tool": "search_text"}),
        envelopes.event("s1", 2, name="tool_result", data={"hitCount": 1}),
        envelopes.event("s1", 3, name="citation", data={"documentName": "taxes.pdf"}),
        envelopes.delta("s1", 4, {"text": "The 2024 deduction is $14,600."}),
        envelopes.done("s1", 5, data={"messageId": message_id, "citationCount": 1}),
    ]


async def test_transcript_sink_maps_envelopes_to_step_kinds() -> None:
    """Each transcript-worthy envelope maps to its kind; start/done are framing (skipped)."""
    sink = RunTranscriptSink(stream_id="s1")
    mid = "11111111-1111-1111-1111-111111111111"
    await _publish_all(sink, "s1", _stream(mid))

    # The sink captured every envelope in order.
    assert [e["seq"] for e in sink.envelopes] == [0, 1, 2, 3, 4, 5]

    # persist() would write these kinds (start/done are skipped). We assert the
    # kind mapping directly via the private helper the persistence uses.
    from app.services.run_sink import _step_kind

    kinds = [_step_kind(e) for e in sink.envelopes]
    assert kinds == [
        None,  # start — framing
        RunStepKind.TOOL_CALL,
        RunStepKind.TOOL_RESULT,
        RunStepKind.CITATION,
        RunStepKind.DELTA,
        None,  # done — framing
    ]


async def test_transcript_sink_outcome_succeeded_with_summary() -> None:
    sink = RunTranscriptSink(stream_id="s1")
    mid = "22222222-2222-2222-2222-222222222222"
    await _publish_all(sink, "s1", _stream(mid))

    outcome = sink.outcome()
    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.error is None
    assert outcome.summary and "14,600" in outcome.summary
    assert str(sink.message_id()) == mid


async def test_transcript_sink_outcome_failed_with_typed_error() -> None:
    """An ``error`` terminal folds into ``failed`` with a typed {code, message}."""
    sink = RunTranscriptSink(stream_id="s1")
    await _publish_all(
        sink,
        "s1",
        [
            envelopes.start("s1", 0, data={"messageId": None}),
            envelopes.error(
                "s1",
                1,
                {
                    "title": "Bad Gateway",
                    "status": 502,
                    "code": "model_unavailable",
                    "detail": "The model was unavailable.",
                },
            ),
        ],
    )
    outcome = sink.outcome()
    assert outcome.status is RunStatus.FAILED
    assert outcome.summary is None
    assert outcome.error is not None
    assert outcome.error.code == "model_unavailable"
    assert outcome.error.message == "The model was unavailable."


async def test_transcript_sink_no_answer_yields_placeholder_summary() -> None:
    sink = RunTranscriptSink(stream_id="s1")
    await _publish_all(
        sink,
        "s1",
        [
            envelopes.start("s1", 0, data={"messageId": None}),
            envelopes.done("s1", 1, data={"citationCount": 0}),
        ],
    )
    outcome = sink.outcome()
    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.summary  # a stable placeholder, never empty


# --- #578: a denied/unavailable tool can never fold into Succeeded ----------


def _tool_result(seq: int, *, ok: bool, error: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"callId": f"c{seq}", "tool": "run_python", "ok": ok}
    if error is not None:
        data["error"] = error
    return envelopes.event("s1", seq, name="tool_result", data=data)


def _done_stream(*middle: dict[str, Any]) -> list[dict[str, Any]]:
    envs = [envelopes.start("s1", 0, data={"messageId": None})]
    envs.extend(middle)
    envs.append(envelopes.delta("s1", 90, {"text": "A partial answer."}))
    envs.append(envelopes.done("s1", 91, data={"citationCount": 0}))
    return envs


async def test_denied_tool_result_cannot_fold_into_succeeded() -> None:
    """A ``code_execution_denied`` tool result + ``done`` is Failed/tool_failure (#578)."""
    sink = RunTranscriptSink(stream_id="s1")
    await _publish_all(
        sink, "s1", _done_stream(_tool_result(1, ok=False, error="code_execution_denied"))
    )
    outcome = sink.outcome()
    assert outcome.status is RunStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "tool_failure"


async def test_approval_and_missing_tool_results_cannot_fold_into_succeeded() -> None:
    """Policy/approval denials and a missing adapter are tool failures too (#578)."""
    for code in ("approval_denied", "tool_not_permitted", "tool_not_found"):
        sink = RunTranscriptSink(stream_id="s1")
        await _publish_all(sink, "s1", _done_stream(_tool_result(1, ok=False, error=code)))
        outcome = sink.outcome()
        assert outcome.status is RunStatus.FAILED, code
        assert outcome.error is not None and outcome.error.code == "tool_failure", code


async def test_unavailable_tool_synonym_cannot_fold_into_succeeded() -> None:
    """An unenumerated unavailable/disabled spelling still fails closed (#578)."""
    for code in ("sandbox_unavailable", "web_search_disabled"):
        sink = RunTranscriptSink(stream_id="s1")
        await _publish_all(sink, "s1", _done_stream(_tool_result(1, ok=False, error=code)))
        outcome = sink.outcome()
        assert outcome.status is RunStatus.FAILED, code
        assert outcome.error is not None and outcome.error.code == "tool_failure", code


async def test_recoverable_tool_error_can_still_succeed() -> None:
    """A recoverable error the model moved past (bad args/timeout) still succeeds (#578)."""
    for code in ("tool_bad_args", "tool_timeout", "tool_error", "service_unavailable"):
        sink = RunTranscriptSink(stream_id="s1")
        await _publish_all(sink, "s1", _done_stream(_tool_result(1, ok=False, error=code)))
        outcome = sink.outcome()
        assert outcome.status is RunStatus.SUCCEEDED, code
        assert outcome.error is None, code


async def test_any_denied_tool_among_successes_fails_the_run() -> None:
    sink = RunTranscriptSink(stream_id="s1")
    await _publish_all(
        sink,
        "s1",
        _done_stream(
            _tool_result(1, ok=True),
            _tool_result(2, ok=False, error="autonomy_denied"),
        ),
    )
    outcome = sink.outcome()
    assert outcome.status is RunStatus.FAILED
    assert outcome.error is not None and outcome.error.code == "tool_failure"


async def test_denial_remains_tool_failure_when_the_stream_also_errors() -> None:
    """Any denied capability is Failed/tool_failure, including an errored stream."""
    sink = RunTranscriptSink(stream_id="s1")
    await _publish_all(
        sink,
        "s1",
        [
            envelopes.start("s1", 0, data={"messageId": None}),
            _tool_result(1, ok=False, error="code_execution_denied"),
            envelopes.error(
                "s1",
                2,
                {"code": "model_unavailable", "detail": "The model was unavailable."},
            ),
        ],
    )
    outcome = sink.outcome()
    assert outcome.status is RunStatus.FAILED
    assert outcome.error is not None and outcome.error.code == "tool_failure"


async def test_tee_sink_fans_out_to_every_sink() -> None:
    """A watched run dual-publishes: every wrapped sink receives each envelope (ADR-0015 §3)."""
    transcript = RunTranscriptSink(stream_id="s1")
    from app.realtime.backplane import InMemoryBackplane

    redis_like = InMemoryBackplane()
    tee = TeeSink([transcript, redis_like])

    mid = "33333333-3333-3333-3333-333333333333"
    await _publish_all(tee, "s1", _stream(mid))

    # The transcript captured the full stream.
    assert len(transcript.envelopes) == 6
    # And the live (redis-like) leg replays the same stream to a late subscriber.
    relayed = [env async for env in redis_like.subscribe("s1")]
    assert [e["seq"] for e in relayed] == [0, 1, 2, 3, 4, 5]
    assert relayed[-1]["type"] == "done"
