"""Pure media/transcript policy plus FFmpeg worker helpers (spec 0008 §3–§5).

Provider calls remain in :mod:`app.llm`; storage/DB calls remain in their owning
adapters.  This module owns container validation, a zero-based audio timeline,
bounded chunk plans, overlap stitching, and high-precision contextual names.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from app.domain.llm import Transcription
from app.ingestion.chunking import chunk_text

AUDIO_MIME_TYPES: frozenset[str] = frozenset(
    {
        "audio/aac",
        "audio/flac",
        "audio/mp4",
        "audio/mpeg",
        "audio/ogg",
        "audio/wav",
        "audio/webm",
        "audio/x-wav",
    }
)
VIDEO_MIME_TYPES: frozenset[str] = frozenset({"video/mp4", "video/webm"})
_CONTAINER_FORMATS: dict[str, frozenset[str]] = {
    "audio/aac": frozenset({"aac"}),
    "audio/flac": frozenset({"flac"}),
    "audio/mp4": frozenset({"mov", "mp4", "m4a"}),
    "audio/mpeg": frozenset({"mp3"}),
    "audio/ogg": frozenset({"ogg"}),
    "audio/wav": frozenset({"wav"}),
    "audio/webm": frozenset({"matroska", "webm"}),
    "audio/x-wav": frozenset({"wav"}),
    "video/mp4": frozenset({"mov", "mp4"}),
    "video/webm": frozenset({"matroska", "webm"}),
}


class MediaProcessingError(Exception):
    """Uploaded media cannot produce trustworthy time-aligned evidence."""


@dataclass(frozen=True, slots=True)
class MediaProbe:
    kind: str
    duration_ms: int
    has_audio: bool
    has_video: bool
    audio_offset_ms: int


@dataclass(frozen=True, slots=True)
class MediaSpan:
    index: int
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class ChunkTranscription:
    span: MediaSpan
    result: Transcription


@dataclass(frozen=True, slots=True)
class StitchedWord:
    text: str
    start_ms: int
    end_ms: int
    speaker_id: str
    confidence: float | None


@dataclass(frozen=True, slots=True)
class TranscriptSegmentDraft:
    id: UUID
    ordinal: int
    speaker_id: str
    start_ms: int
    end_ms: int
    char_start: int
    char_end: int
    text: str
    confidence: float | None


class SpeakerNameStatus(str, Enum):
    UNKNOWN = "unknown"
    INFERRED = "inferred"


class SpeakerNameMethod(str, Enum):
    SELF_INTRODUCTION = "self_introduction"
    CONTEXTUAL_DIALOGUE = "contextual_dialogue"


@dataclass(frozen=True, slots=True)
class SpeakerIdentity:
    speaker_id: str
    display_name: str | None
    status: SpeakerNameStatus
    confidence: float | None
    method: SpeakerNameMethod | None
    evidence_segment_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class TranscriptChunkDraft:
    text: str
    char_start: int
    char_end: int
    time_start_ms: int
    time_end_ms: int
    transcript_segment_id: UUID | None
    speaker_id: str | None
    speaker_name: str | None


async def _run_process(*command: str) -> bytes:
    def _invoke() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # noqa: S603 - argv only; never a shell command
            command,
            check=False,
            capture_output=True,
        )

    try:
        # This module is called only from a Celery worker. ``to_thread`` keeps the
        # async orchestration non-blocking and also works under Windows' selector
        # test loop, which intentionally has no subprocess transport.
        completed = await asyncio.to_thread(_invoke)
    except OSError as exc:
        raise MediaProcessingError(f"media tool could not start: {Path(command[0]).name}") from exc
    if completed.returncode != 0:
        raise MediaProcessingError(
            f"media tool failed: {Path(command[0]).name} (exit {completed.returncode})"
        )
    return completed.stdout


def _finite_number(value: object, *, allow_negative: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if (
        (number < 0 and not allow_negative)
        or number != number
        or number in {float("inf"), float("-inf")}
    ):
        return None
    return number


def _parse_probe(payload: object, *, mime_type: str, max_duration_ms: int) -> MediaProbe:
    normalized = mime_type.split(";", 1)[0].strip().lower()
    if normalized in AUDIO_MIME_TYPES:
        kind = "audio"
    elif normalized in VIDEO_MIME_TYPES:
        kind = "video"
    else:
        raise MediaProcessingError(f"unsupported media MIME type: {normalized!r}")
    if not isinstance(payload, dict):
        raise MediaProcessingError("ffprobe response was not an object")
    streams = payload.get("streams")
    raw_format = payload.get("format")
    if not isinstance(streams, list) or not isinstance(raw_format, dict):
        raise MediaProcessingError("ffprobe response omitted streams or format")
    format_name = raw_format.get("format_name")
    if not isinstance(format_name, str):
        raise MediaProcessingError("media container format is missing")
    detected_formats = {value.strip().lower() for value in format_name.split(",")}
    if not detected_formats.intersection(_CONTAINER_FORMATS[normalized]):
        raise MediaProcessingError("media container does not match its declared MIME type")

    typed_streams = [stream for stream in streams if isinstance(stream, dict)]
    audio_streams = [stream for stream in typed_streams if stream.get("codec_type") == "audio"]
    video_streams = [stream for stream in typed_streams if stream.get("codec_type") == "video"]
    moving_video_streams = []
    for stream in video_streams:
        disposition = stream.get("disposition")
        attached_pic = disposition.get("attached_pic") if isinstance(disposition, dict) else None
        if isinstance(attached_pic, bool) or attached_pic != 1:
            moving_video_streams.append(stream)
    if not audio_streams:
        raise MediaProcessingError("media has no decodable audio stream")
    if kind == "video" and not moving_video_streams:
        raise MediaProcessingError("declared video has no decodable video stream")
    if kind == "audio" and moving_video_streams:
        raise MediaProcessingError("declared audio contains a video stream")

    duration = _finite_number(raw_format.get("duration"))
    if duration is None:
        durations = [
            candidate
            for stream in typed_streams
            if (candidate := _finite_number(stream.get("duration"))) is not None
        ]
        duration = max(durations, default=None)
    if duration is None or duration <= 0:
        raise MediaProcessingError("media duration is missing or empty")
    duration_ms = round(duration * 1000)
    if duration_ms <= 0 or duration_ms > max_duration_ms:
        raise MediaProcessingError("media duration exceeds the configured limit")

    # Attached artwork is not part of the playable time axis and may carry a
    # synthetic timestamp. Only audible/moving streams establish presentation
    # zero when the container itself has no start_time.
    timeline_streams = [*audio_streams, *moving_video_streams]
    starts = [
        candidate
        for stream in timeline_streams
        if (candidate := _finite_number(stream.get("start_time"), allow_negative=True)) is not None
    ]
    format_start = _finite_number(raw_format.get("start_time"), allow_negative=True)
    presentation_start = format_start if format_start is not None else min(starts, default=0.0)
    audio_start = _finite_number(audio_streams[0].get("start_time"), allow_negative=True)
    effective_audio_start = audio_start if audio_start is not None else presentation_start
    audio_offset_ms = round((effective_audio_start - presentation_start) * 1000)
    if audio_offset_ms >= duration_ms:
        raise MediaProcessingError("audio starts outside the media duration")
    return MediaProbe(
        kind=kind,
        duration_ms=duration_ms,
        has_audio=True,
        has_video=bool(moving_video_streams),
        audio_offset_ms=audio_offset_ms,
    )


async def probe_media(
    path: Path,
    *,
    mime_type: str,
    ffprobe_path: str,
    max_duration_ms: int,
) -> MediaProbe:
    output = await _run_process(
        ffprobe_path,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    )
    try:
        payload: Any = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaProcessingError("ffprobe returned invalid JSON") from exc
    return _parse_probe(payload, mime_type=mime_type, max_duration_ms=max_duration_ms)


async def normalize_media_audio(
    source: Path,
    destination: Path,
    *,
    probe: MediaProbe,
    ffmpeg_path: str,
) -> None:
    """Extract/transcode mono PCM16 at 16 kHz while preserving player offset."""
    if source.resolve() == destination.resolve():
        raise MediaProcessingError("normalized audio destination must differ from source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if probe.audio_offset_ms >= 0:
        align = (
            f"asetpts=PTS-STARTPTS,adelay={probe.audio_offset_ms}:all=1,"
            if probe.audio_offset_ms
            else "asetpts=PTS-STARTPTS,"
        )
    else:
        # The track has pre-roll before player time zero: remove it before
        # resetting timestamps instead of shifting later speech out of place.
        align = f"atrim=start={-probe.audio_offset_ms / 1000:.3f},asetpts=PTS-STARTPTS,"
    duration_seconds = probe.duration_ms / 1000
    # Pad/trim to the player's exact duration. Otherwise a video whose audio
    # ends early creates a final planned chunk with no file bytes, or an encoder
    # tail can produce provider words outside the citable media duration.
    audio_filter = (
        f"{align}aresample=16000:async=1:first_pts=0," f"apad,atrim=duration={duration_seconds:.3f}"
    )
    await _run_process(
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        audio_filter,
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(destination),
    )
    if not destination.is_file() or destination.stat().st_size <= 44:
        raise MediaProcessingError("FFmpeg produced no normalized audio")


def plan_audio_chunks(
    *, duration_ms: int, max_chunk_ms: int, overlap_ms: int
) -> tuple[MediaSpan, ...]:
    if duration_ms <= 0 or max_chunk_ms <= 0:
        raise MediaProcessingError("audio duration and chunk size must be positive")
    if overlap_ms <= 0 or overlap_ms >= max_chunk_ms:
        raise MediaProcessingError(
            "audio chunk overlap must be positive and smaller than the chunk size"
        )
    spans: list[MediaSpan] = []
    start = 0
    while start < duration_ms:
        end = min(duration_ms, start + max_chunk_ms)
        spans.append(MediaSpan(index=len(spans), start_ms=start, end_ms=end))
        if end == duration_ms:
            break
        start = end - overlap_ms
    return tuple(spans)


async def extract_audio_chunk(
    normalized_audio: Path,
    destination: Path,
    *,
    span: MediaSpan,
    ffmpeg_path: str,
) -> None:
    if not (0 <= span.start_ms < span.end_ms):
        raise MediaProcessingError("audio chunk has an invalid span")
    destination.parent.mkdir(parents=True, exist_ok=True)
    await _run_process(
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{span.start_ms / 1000:.3f}",
        "-i",
        str(normalized_audio),
        "-t",
        f"{(span.end_ms - span.start_ms) / 1000:.3f}",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(destination),
    )


def _normal_word(text: str) -> str:
    return re.sub(r"[^\w']+", "", text.casefold())


def stitch_chunk_transcriptions(
    chunks: Sequence[ChunkTranscription], *, duration_ms: int
) -> tuple[StitchedWord, ...]:
    """Merge overlapping results without inventing cross-chunk speaker identity.

    A provider's diarizer labels are request-local.  Labels are linked across an
    adjacent overlap only when matching word+time evidence supports the mapping;
    an unmatched label receives a new neutral file-local id.
    """
    if duration_ms <= 0 or not chunks:
        raise MediaProcessingError("cannot stitch an empty media transcript")
    ordered = sorted(chunks, key=lambda item: item.span.index)
    for expected, item in enumerate(ordered):
        if item.span.index != expected or not (0 <= item.span.start_ms < item.span.end_ms):
            raise MediaProcessingError("transcription chunks are missing or out of order")
        if item.span.end_ms > duration_ms:
            raise MediaProcessingError("transcription chunk exceeds media duration")
        if expected and item.span.start_ms >= ordered[expected - 1].span.end_ms:
            raise MediaProcessingError("adjacent transcription chunks do not overlap")

    next_speaker = 1
    label_maps: list[dict[str, str]] = []
    raw_global: list[list[tuple[str, int, int, str, float | None]]] = []
    for item in ordered:
        duration = item.span.end_ms - item.span.start_ms
        global_words: list[tuple[str, int, int, str, float | None]] = []
        previous_local_start = -1
        for word in item.result.words:
            if (
                not (0 <= word.start_ms < word.end_ms <= duration)
                or word.start_ms < previous_local_start
            ):
                raise MediaProcessingError("provider word falls outside its audio chunk")
            previous_local_start = word.start_ms
            start = item.span.start_ms + word.start_ms
            end = item.span.start_ms + word.end_ms
            if end > duration_ms:
                raise MediaProcessingError("provider word falls outside media duration")
            global_words.append((word.text, start, end, word.speaker_label, word.confidence))
        if not global_words:
            raise MediaProcessingError("provider returned an empty transcription chunk")
        raw_global.append(global_words)

        mapping: dict[str, str] = {}
        labels = list(dict.fromkeys(word[3] for word in global_words))
        if label_maps:
            previous = raw_global[-2]
            prior_map = label_maps[-1]
            counts: dict[str, Counter[str]] = defaultdict(Counter)
            used_previous: set[int] = set()
            for text, start, end, label, _confidence in global_words:
                normalized = _normal_word(text)
                if not normalized:
                    continue
                word_matches = [
                    (abs(start - p_start) + abs(end - p_end), index, p_label)
                    for index, (
                        p_text,
                        p_start,
                        p_end,
                        p_label,
                        _p_confidence,
                    ) in enumerate(previous)
                    if index not in used_previous
                    and normalized == _normal_word(p_text)
                    and abs(start - p_start) <= 750
                    and abs(end - p_end) <= 750
                ]
                if word_matches:
                    _distance, prior_index, prior_label = min(word_matches)
                    used_previous.add(prior_index)
                    counts[label][prior_map[prior_label]] += 1
            claimed: set[str] = set()
            ranked: list[tuple[int, str, str]] = []
            for label, candidates in counts.items():
                if not candidates:
                    continue
                best = candidates.most_common()
                # One common word at a boundary is not enough evidence to join
                # request-local diarizer identities. A false split is safer
                # than falsely merging two people under one file-local id.
                if best[0][1] >= 2 and (len(best) == 1 or best[0][1] > best[1][1]):
                    ranked.append((best[0][1], label, best[0][0]))
            for _score, label, canonical in sorted(ranked, reverse=True):
                if canonical not in claimed:
                    mapping[label] = canonical
                    claimed.add(canonical)
        for label in labels:
            if label not in mapping:
                mapping[label] = f"speaker-{next_speaker}"
                next_speaker += 1
        label_maps.append(mapping)

    boundaries = [
        (ordered[index].span.end_ms + ordered[index + 1].span.start_ms) // 2
        for index in range(len(ordered) - 1)
    ]
    stitched: list[StitchedWord] = []
    for index, words in enumerate(raw_global):
        ownership_start = 0 if index == 0 else boundaries[index - 1]
        ownership_end = duration_ms + 1 if index == len(raw_global) - 1 else boundaries[index]
        for text, start, end, label, confidence in words:
            midpoint = (start + end) // 2
            if ownership_start <= midpoint < ownership_end:
                stitched.append(
                    StitchedWord(
                        text=text,
                        start_ms=start,
                        end_ms=end,
                        speaker_id=label_maps[index][label],
                        confidence=confidence,
                    )
                )
    stitched.sort(key=lambda word: (word.start_ms, word.end_ms))
    if not stitched:
        raise MediaProcessingError("overlap stitching produced no words")
    return tuple(stitched)


def _join_tokens(tokens: Sequence[str]) -> str:
    text = ""
    for raw in tokens:
        token = raw.strip()
        if not token:
            continue
        if not text or re.match(r"^[,.;:!?%\)\]}]", token) or text.endswith(("(", "[", "{")):
            text += token
        else:
            text += f" {token}"
    return text


def build_transcript_segments(
    words: Sequence[StitchedWord], *, max_gap_ms: int = 1_500
) -> tuple[TranscriptSegmentDraft, ...]:
    if not words:
        raise MediaProcessingError("cannot build transcript segments without words")
    groups: list[list[StitchedWord]] = []
    for word in words:
        if not (0 <= word.start_ms < word.end_ms) or not word.text.strip():
            raise MediaProcessingError("stitched word has invalid evidence")
        if (
            not groups
            or groups[-1][-1].speaker_id != word.speaker_id
            or word.start_ms - groups[-1][-1].end_ms > max_gap_ms
        ):
            groups.append([word])
        else:
            groups[-1].append(word)

    segments: list[TranscriptSegmentDraft] = []
    cursor = 0
    for ordinal, group in enumerate(groups):
        text = _join_tokens([word.text for word in group])
        if not text:
            continue
        confidences = [word.confidence for word in group if word.confidence is not None]
        confidence = sum(confidences) / len(confidences) if confidences else None
        segment = TranscriptSegmentDraft(
            id=uuid4(),
            ordinal=ordinal,
            speaker_id=group[0].speaker_id,
            start_ms=min(word.start_ms for word in group),
            end_ms=max(word.end_ms for word in group),
            char_start=cursor,
            char_end=cursor + len(text),
            text=text,
            confidence=confidence,
        )
        segments.append(segment)
        cursor = segment.char_end + 1  # canonical transcript joins turns with "\n"
    if not segments:
        raise MediaProcessingError("transcript segmentation produced no text")
    return tuple(segments)


_NAME_TOKEN = r"[A-Z][A-Za-z'’\-]*"
_FULL_NAME = rf"({_NAME_TOKEN}(?:\s+{_NAME_TOKEN}){{0,2}})"
_SELF_PATTERNS = (
    re.compile(
        rf"(?:^|[.!?]\s*)(?:(?i:oh)[,!]?\s+)?(?:(?i:hello|hi|hey)[,!]?\s+)?"
        rf"(?i:my name is)\s+{_FULL_NAME}(?=[,.!?]|$)"
    ),
    re.compile(
        rf"(?:^|[.!?]\s*)(?:(?i:hello|hi|hey)[,!]?\s+)?"
        rf"(?i:this is)\s+{_FULL_NAME}(?=[,.!?]|\s+[a-z]|$)"
    ),
    re.compile(
        rf"(?:^|[.!?]\s*)(?i:hello|hi|hey)[,!]?\s+" rf"(?i:I am|I'm)\s+{_FULL_NAME}(?=[,.!?]|$)"
    ),
)
_ADDRESS_PATTERNS = (
    re.compile(rf"^(?i:hello|hi|hey|thanks|thank you|welcome)[,! ]+{_FULL_NAME}[,.!?]?\s*$"),
    re.compile(rf"^(?i:over to you)[,! ]+{_FULL_NAME}[,.!?]?\s*$"),
    re.compile(
        rf"^{_FULL_NAME},\s+(?:can|could|would|will|are|is|do|did|please|thanks|thank|what|how)\b"
    ),
)
_NON_NAMES = frozenset(
    {
        "everyone",
        "everybody",
        "team",
        "folks",
        "there",
        "all",
        "sir",
        "madam",
    }
)


def _valid_name(raw: str) -> str | None:
    name = " ".join(raw.strip(" ,.!?").split())
    parts = name.split()
    if not 1 <= len(parts) <= 3:
        return None
    if any(part.casefold() in _NON_NAMES or not re.fullmatch(_NAME_TOKEN, part) for part in parts):
        return None
    return name


def _self_name(text: str) -> str | None:
    matches = {
        name
        for pattern in _SELF_PATTERNS
        for match in pattern.finditer(text)
        if (name := _valid_name(match.group(1))) is not None
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _addressed_name(text: str) -> str | None:
    matches = {
        name
        for pattern in _ADDRESS_PATTERNS
        if (match := pattern.search(text)) is not None
        if (name := _valid_name(match.group(1))) is not None
    }
    return next(iter(matches)) if len(matches) == 1 else None


def infer_speaker_names(
    segments: Sequence[TranscriptSegmentDraft],
) -> tuple[SpeakerIdentity, ...]:
    """Infer display-only names from explicit local transcript evidence.

    Conflicting claims, ambiguous addresses, and duplicate names across diarizer
    ids all fall back to neutral ``speaker-N`` labels.
    """
    ordered = sorted(segments, key=lambda segment: segment.ordinal)
    speakers = sorted({segment.speaker_id for segment in ordered})
    candidates: dict[str, list[tuple[str, float, SpeakerNameMethod, tuple[UUID, ...]]]] = (
        defaultdict(list)
    )
    for segment in ordered:
        name = _self_name(segment.text)
        if name is not None:
            candidates[segment.speaker_id].append(
                (name, 0.98, SpeakerNameMethod.SELF_INTRODUCTION, (segment.id,))
            )

    for index, segment in enumerate(ordered[:-1]):
        name = _addressed_name(segment.text)
        response = ordered[index + 1]
        if (
            name is not None
            and response.speaker_id != segment.speaker_id
            and 0 <= response.start_ms - segment.end_ms <= 10_000
        ):
            candidates[response.speaker_id].append(
                (
                    name,
                    0.80,
                    SpeakerNameMethod.CONTEXTUAL_DIALOGUE,
                    (segment.id, response.id),
                )
            )

    chosen: dict[str, tuple[str, float, SpeakerNameMethod, tuple[UUID, ...]]] = {}
    for speaker, values in candidates.items():
        unique_names = {value[0].casefold() for value in values}
        if len(unique_names) == 1:
            chosen[speaker] = max(values, key=lambda value: value[1])
    duplicates = Counter(value[0].casefold() for value in chosen.values())

    identities: list[SpeakerIdentity] = []
    for speaker in speakers:
        value = chosen.get(speaker)
        if value is None or duplicates[value[0].casefold()] > 1:
            identities.append(
                SpeakerIdentity(
                    speaker_id=speaker,
                    display_name=None,
                    status=SpeakerNameStatus.UNKNOWN,
                    confidence=None,
                    method=None,
                    evidence_segment_ids=(),
                )
            )
        else:
            identities.append(
                SpeakerIdentity(
                    speaker_id=speaker,
                    display_name=value[0],
                    status=SpeakerNameStatus.INFERRED,
                    confidence=value[1],
                    method=value[2],
                    evidence_segment_ids=value[3],
                )
            )
    return tuple(identities)


def transcript_text(segments: Sequence[TranscriptSegmentDraft]) -> str:
    text = "\n".join(segment.text for segment in segments)
    for segment in segments:
        if text[segment.char_start : segment.char_end] != segment.text:
            raise MediaProcessingError("transcript character offsets are inconsistent")
    return text


def build_transcript_chunks(
    segments: Sequence[TranscriptSegmentDraft],
    identities: Sequence[SpeakerIdentity],
    *,
    duration_ms: int,
    chunk_size: int,
    overlap: int,
) -> tuple[TranscriptChunkDraft, ...]:
    """Create retrieval chunks with paired, bounded timestamp evidence."""
    if not segments or duration_ms <= 0:
        raise MediaProcessingError("media transcript has no bounded segments")
    segment_ids = {segment.id for segment in segments}
    if len(segment_ids) != len(segments):
        raise MediaProcessingError("transcript segment ids are not unique")
    for segment in segments:
        if not (0 <= segment.start_ms < segment.end_ms <= duration_ms):
            raise MediaProcessingError("transcript segment is outside media duration")
    names = {identity.speaker_id: identity.display_name for identity in identities}
    chunks = chunk_text(transcript_text(segments), chunk_size=chunk_size, overlap=overlap)
    drafts: list[TranscriptChunkDraft] = []
    for chunk in chunks:
        covered = [
            segment
            for segment in segments
            if segment.char_end > chunk.char_start and segment.char_start < chunk.char_end
        ]
        if not covered or any(segment.id not in segment_ids for segment in covered):
            raise MediaProcessingError("retrieval chunk has no same-document transcript segment")
        speakers = {segment.speaker_id for segment in covered}
        speaker_id = next(iter(speakers)) if len(speakers) == 1 else None
        start_ms = min(segment.start_ms for segment in covered)
        end_ms = max(segment.end_ms for segment in covered)
        if not (0 <= start_ms < end_ms <= duration_ms):
            raise MediaProcessingError("retrieval chunk has an invalid timestamp pair")
        drafts.append(
            TranscriptChunkDraft(
                text=chunk.text,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                time_start_ms=start_ms,
                time_end_ms=end_ms,
                # A chunk spanning turns has an overall time envelope but no
                # single segment can truthfully own that entire envelope.
                transcript_segment_id=covered[0].id if len(covered) == 1 else None,
                speaker_id=speaker_id,
                speaker_name=names.get(speaker_id) if speaker_id is not None else None,
            )
        )
    return tuple(drafts)


__all__ = [
    "AUDIO_MIME_TYPES",
    "VIDEO_MIME_TYPES",
    "ChunkTranscription",
    "MediaProbe",
    "MediaProcessingError",
    "MediaSpan",
    "SpeakerIdentity",
    "SpeakerNameMethod",
    "SpeakerNameStatus",
    "StitchedWord",
    "TranscriptChunkDraft",
    "TranscriptSegmentDraft",
    "build_transcript_chunks",
    "build_transcript_segments",
    "extract_audio_chunk",
    "infer_speaker_names",
    "normalize_media_audio",
    "plan_audio_chunks",
    "probe_media",
    "stitch_chunk_transcriptions",
    "transcript_text",
]
