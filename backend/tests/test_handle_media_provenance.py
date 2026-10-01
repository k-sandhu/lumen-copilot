"""Media provenance participates in stable, private source-handle identity."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from app.domain.chat import GroundedCitation
from app.domain.retrieval import RetrievedPassage
from app.services.chat_runtime import _citation_event_data
from app.services.tools.handles import EvidenceHandles


def _passage() -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=uuid.UUID("00000000-0000-0000-0000-000000000061"),
        document_id=uuid.UUID("00000000-0000-0000-0000-000000000062"),
        document_name="meeting.mp4",
        ord=4,
        text="Private transcript sentence.",
        char_start=120,
        char_end=149,
        score=0.91,
        time_start_ms=61_250,
        time_end_ms=64_900,
        transcript_segment_id=uuid.UUID("00000000-0000-0000-0000-000000000063"),
        speaker_id="speaker-2",
        speaker_name="Private Speaker Name",
    )


def test_media_provenance_changes_source_handle_identity_without_storing_plaintext() -> None:
    handles = EvidenceHandles()
    passage = _passage()

    original = handles.passage(passage)
    assert handles.passage(passage) == original

    changed_timestamp = handles.passage(replace(passage, time_start_ms=61_251))
    changed_end_timestamp = handles.passage(replace(passage, time_end_ms=64_901))
    changed_segment = handles.passage(
        replace(passage, transcript_segment_id=uuid.UUID("00000000-0000-0000-0000-000000000064"))
    )
    changed_speaker = handles.passage(replace(passage, speaker_id="speaker-3"))
    changed_speaker_name = handles.passage(replace(passage, speaker_name="Another Private Name"))

    assert (
        len(
            {
                original,
                changed_timestamp,
                changed_end_timestamp,
                changed_segment,
                changed_speaker,
                changed_speaker_name,
            }
        )
        == 6
    )
    assert handles.passage(passage) == original

    entries = [
        handles.resolve(handle)
        for handle in (
            original,
            changed_timestamp,
            changed_end_timestamp,
            changed_segment,
            changed_speaker,
            changed_speaker_name,
        )
    ]
    serialized = json.dumps(entries, sort_keys=True)
    assert passage.text not in serialized
    assert passage.speaker_name not in serialized
    assert passage.speaker_id not in serialized
    assert all(entry is not None for entry in entries)

    document_handle = handles.document(passage.document_id)
    assert handles.document(passage.document_id) == document_handle
    assert handles.resolve(document_handle) == {
        "kind": "document",
        "document_id": str(passage.document_id),
    }


def test_media_citation_keeps_source_handle_and_matches_websocket_contract() -> None:
    citation = replace(
        GroundedCitation.from_passage(_passage()),
        id=uuid.UUID("00000000-0000-0000-0000-000000000065"),
        handle="S1",
    )
    payload = _citation_event_data(citation)

    assert payload["handle"] == "S1"
    assert payload["timeStartMs"] == 61_250
    assert payload["timeEndMs"] == 64_900
    assert payload["transcriptSegmentId"] == str(citation.transcript_segment_id)
    assert payload["speakerId"] == citation.speaker_id
    assert payload["speakerName"] == citation.speaker_name

    contract_path = Path(__file__).parents[2] / "contracts" / "websocket-envelopes.schema.json"
    schema = json.loads(contract_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(
        schema["$defs"]["ChatCitation"], format_checker=FormatChecker()
    )
    assert validator.is_valid(payload)


def test_malformed_media_citation_is_rejected_before_payload_is_emitted() -> None:
    citation = replace(
        GroundedCitation.from_passage(_passage()),
        handle="S1",
        time_end_ms=61_250,
    )
    emitted: list[dict[str, object]] = []

    with pytest.raises(ValueError, match="timestamps must be ordered"):
        emitted.append(_citation_event_data(citation))

    assert emitted == []
