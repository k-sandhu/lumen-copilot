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
                {"title": "Bad Gateway", "status": 502, "code": "model_unavailable",
                 "detail": "The model was unavailable."},
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
