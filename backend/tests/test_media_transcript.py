"""Chunk stitching, transcript segments, and conservative contextual names."""

from __future__ import annotations

from uuid import uuid4

from app.domain.llm import Transcription, TranscriptionWord
from app.ingestion.media import (
    ChunkTranscription,
    MediaSpan,
    SpeakerNameMethod,
    StitchedWord,
    build_transcript_chunks,
    build_transcript_segments,
    infer_speaker_names,
    stitch_chunk_transcriptions,
)


def _word(text: str, start: int, end: int, speaker: str) -> TranscriptionWord:
    return TranscriptionWord(
        text=text,
        start_ms=start,
        end_ms=end,
        speaker_label=speaker,
        confidence=None,
    )


def _result(*words: TranscriptionWord) -> Transcription:
    return Transcription(
        text=" ".join(word.text for word in words),
        words=words,
        language="en",
        model="stt",
    )


def test_overlap_stitching_deduplicates_words_and_maps_speaker_by_evidence() -> None:
    chunks = (
        ChunkTranscription(
            span=MediaSpan(0, 0, 10_000),
            result=_result(
                _word("Opening", 1_000, 1_400, "provider-a"),
                _word("Alice", 9_000, 9_300, "provider-a"),
                _word("hello", 9_400, 9_800, "provider-a"),
            ),
        ),
        ChunkTranscription(
            span=MediaSpan(1, 9_000, 19_000),
            result=_result(
                _word("Alice", 0, 300, "provider-b"),
                _word("hello", 400, 800, "provider-b"),
                _word("Hi", 1_100, 1_350, "provider-c"),
            ),
        ),
    )
    stitched = stitch_chunk_transcriptions(chunks, duration_ms=19_000)
    assert [word.text for word in stitched] == ["Opening", "Alice", "hello", "Hi"]
    assert stitched[0].speaker_id == "speaker-1"
    assert stitched[1].speaker_id == "speaker-1"
    assert stitched[2].speaker_id == "speaker-1"
    assert stitched[3].speaker_id == "speaker-2"
    assert all(a.start_ms <= b.start_ms for a, b in zip(stitched, stitched[1:], strict=False))


def test_self_introduction_is_inferred_but_conflict_returns_neutral_label() -> None:
    speaker_one = "speaker-1"
    speaker_two = "speaker-2"
    segments = build_transcript_segments(
        (
            # The segment builder groups only contiguous words by speaker.
            *(
                StitchedWord(
                    text=text,
                    start_ms=i * 100,
                    end_ms=i * 100 + 80,
                    speaker_id=speaker_one,
                    confidence=None,
                )
                for i, text in enumerate(["Hello,", "my", "name", "is", "John", "Smith."])
            ),
            StitchedWord(
                text="Welcome.",
                start_ms=700,
                end_ms=800,
                speaker_id=speaker_two,
                confidence=None,
            ),
        )
    )
    identities = {item.speaker_id: item for item in infer_speaker_names(segments)}
    assert identities[speaker_one].display_name == "John Smith"
    assert identities[speaker_one].method is SpeakerNameMethod.SELF_INTRODUCTION
    assert identities[speaker_one].confidence == 0.98
    assert identities[speaker_one].evidence_segment_ids == (segments[0].id,)
    assert identities[speaker_two].display_name is None

    conflict = (
        segments[0],
        type(segments[0])(
            id=uuid4(),
            ordinal=1,
            speaker_id=speaker_one,
            start_ms=1_000,
            end_ms=1_500,
            char_start=segments[0].char_end + 1,
            char_end=segments[0].char_end + 25,
            text="My name is Michael.",
            confidence=None,
        ),
    )
    assert infer_speaker_names(conflict)[0].display_name is None


def test_filler_before_explicit_self_introduction_remains_high_confidence() -> None:
    segments = build_transcript_segments(
        tuple(
            StitchedWord(
                text=text,
                start_ms=i * 100,
                end_ms=i * 100 + 80,
                speaker_id="speaker-1",
                confidence=None,
            )
            for i, text in enumerate(["Oh,", "hello,", "my", "name", "is", "John."])
        )
    )
    identity = infer_speaker_names(segments)[0]
    assert identity.display_name == "John"
    assert identity.method is SpeakerNameMethod.SELF_INTRODUCTION


def test_single_common_overlap_word_does_not_merge_two_speaker_identities() -> None:
    chunks = (
        ChunkTranscription(
            span=MediaSpan(0, 0, 10_000),
            result=_result(_word("the", 9_200, 9_500, "provider-a")),
        ),
        ChunkTranscription(
            span=MediaSpan(1, 9_000, 19_000),
            result=_result(
                _word("the", 200, 500, "provider-b"),
                _word("answer", 700, 1_100, "provider-b"),
            ),
        ),
    )
    stitched = stitch_chunk_transcriptions(chunks, duration_ms=19_000)
    assert [word.text for word in stitched] == ["the", "answer"]
    assert [word.speaker_id for word in stitched] == ["speaker-1", "speaker-2"]


def test_direct_address_only_names_the_immediate_unambiguous_respondent() -> None:
    from app.ingestion.media import TranscriptSegmentDraft

    address_id = uuid4()
    response_id = uuid4()
    segments = (
        TranscriptSegmentDraft(
            id=address_id,
            ordinal=0,
            speaker_id="speaker-1",
            start_ms=0,
            end_ms=800,
            char_start=0,
            char_end=14,
            text="Thanks, Priya.",
            confidence=None,
        ),
        TranscriptSegmentDraft(
            id=response_id,
            ordinal=1,
            speaker_id="speaker-2",
            start_ms=900,
            end_ms=1_500,
            char_start=15,
            char_end=29,
            text="Happy to help.",
            confidence=None,
        ),
        TranscriptSegmentDraft(
            id=uuid4(),
            ordinal=2,
            speaker_id="speaker-3",
            start_ms=20_000,
            end_ms=21_000,
            char_start=30,
            char_end=48,
            text="Hello Priya and Sam.",
            confidence=None,
        ),
    )
    identities = {item.speaker_id: item for item in infer_speaker_names(segments)}
    priya = identities["speaker-2"]
    assert priya.display_name == "Priya"
    assert priya.method is SpeakerNameMethod.CONTEXTUAL_DIALOGUE
    assert priya.evidence_segment_ids == (address_id, response_id)
    assert identities["speaker-3"].display_name is None


def test_multi_segment_chunk_has_overall_times_without_false_segment_ownership() -> None:
    segments = build_transcript_segments(
        (
            StitchedWord("Hello", 100, 400, "speaker-1", None),
            StitchedWord("Welcome", 500, 900, "speaker-2", None),
        )
    )
    identities = infer_speaker_names(segments)
    chunks = build_transcript_chunks(
        segments,
        identities,
        duration_ms=2_000,
        chunk_size=100,
        overlap=10,
    )
    assert len(chunks) == 1
    assert chunks[0].time_start_ms == 100
    assert chunks[0].time_end_ms == 900
    assert chunks[0].transcript_segment_id is None
    assert chunks[0].speaker_id is None
