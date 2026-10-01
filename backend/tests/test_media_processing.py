"""FFmpeg media validation/extraction and bounded chunk planning (spec 0008 §3)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from app.ingestion.media import (
    MediaProcessingError,
    MediaSpan,
    _parse_probe,
    normalize_media_audio,
    plan_audio_chunks,
    probe_media,
)


def test_chunk_plan_is_overlapping_bounded_and_covers_the_duration() -> None:
    spans = plan_audio_chunks(duration_ms=1_500_000, max_chunk_ms=600_000, overlap_ms=1_000)
    assert spans == (
        MediaSpan(index=0, start_ms=0, end_ms=600_000),
        MediaSpan(index=1, start_ms=599_000, end_ms=1_199_000),
        MediaSpan(index=2, start_ms=1_198_000, end_ms=1_500_000),
    )
    assert all(span.end_ms - span.start_ms <= 600_000 for span in spans)
    assert spans[-1].end_ms == 1_500_000


@pytest.mark.parametrize(
    ("duration_ms", "max_chunk_ms", "overlap_ms"),
    [
        (0, 600_000, 1_000),
        (10_000, 0, 1_000),
        (10_000, 5_000, 0),
        (10_000, 5_000, 5_000),
    ],
)
def test_chunk_plan_rejects_impossible_ranges(
    duration_ms: int, max_chunk_ms: int, overlap_ms: int
) -> None:
    with pytest.raises(MediaProcessingError):
        plan_audio_chunks(
            duration_ms=duration_ms,
            max_chunk_ms=max_chunk_ms,
            overlap_ms=overlap_ms,
        )


def test_audio_probe_allows_attached_cover_art_but_not_moving_video() -> None:
    base = {
        "format": {"format_name": "mp3", "duration": "10.0", "start_time": "0.0"},
        "streams": [{"codec_type": "audio", "start_time": "0.0"}],
    }
    with_cover = {
        **base,
        "streams": [
            *base["streams"],
            {"codec_type": "video", "disposition": {"attached_pic": 1}},
        ],
    }
    probe = _parse_probe(with_cover, mime_type="audio/mpeg", max_duration_ms=20_000)
    assert probe.kind == "audio"
    assert probe.has_video is False

    with_moving_video = {
        **base,
        "streams": [
            *base["streams"],
            {"codec_type": "video", "disposition": {"attached_pic": 0}},
        ],
    }
    with pytest.raises(MediaProcessingError, match="contains a video stream"):
        _parse_probe(with_moving_video, mime_type="audio/mpeg", max_duration_ms=20_000)


def test_video_probe_rejects_cover_art_as_its_only_video_stream() -> None:
    payload = {
        "format": {"format_name": "mov,mp4,m4a", "duration": "10.0"},
        "streams": [
            {"codec_type": "audio"},
            {"codec_type": "video", "disposition": {"attached_pic": 1}},
        ],
    }
    with pytest.raises(MediaProcessingError, match="decodable video stream"):
        _parse_probe(payload, mime_type="video/mp4", max_duration_ms=20_000)


def test_probe_preserves_signed_audio_preroll_offset_for_zero_based_trim() -> None:
    payload = {
        "format": {
            "format_name": "mov,mp4,m4a",
            "duration": "10.0",
            "start_time": "1.0",
        },
        "streams": [{"codec_type": "audio", "start_time": "0.75"}],
    }
    probe = _parse_probe(payload, mime_type="audio/mp4", max_duration_ms=20_000)
    assert probe.audio_offset_ms == -250


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are required for the synthetic media conformance fixture",
)
async def test_video_original_is_probed_and_audio_is_zero_based_mono_16khz(
    tmp_path: Path,
) -> None:
    """A delayed audio track stays aligned to the player's zero-based timeline."""
    source = tmp_path / "meeting.mp4"
    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=2",
            "-itsoffset",
            "0.75",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.75",
            "-shortest",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-y",
            str(source),
        ],
        check=True,
    )

    probe = await probe_media(
        source,
        mime_type="video/mp4",
        ffprobe_path="ffprobe",
        max_duration_ms=8 * 60 * 60 * 1000,
    )
    assert probe.kind == "video"
    assert probe.has_audio is True
    assert probe.has_video is True
    assert probe.duration_ms > 0
    assert probe.audio_offset_ms >= 650

    normalized = tmp_path / "normalized.wav"
    await normalize_media_audio(
        source,
        normalized,
        probe=probe,
        ffmpeg_path="ffmpeg",
    )
    assert source.exists(), "the original video is retained as the viewer reference"
    with wave.open(str(normalized), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getframerate() == 16_000
        assert abs(reader.getnframes() - round(probe.duration_ms * 16)) <= 16
        frames = reader.readframes(reader.getnframes())

    # PCM16 silence should precede the delayed tone. Allow encoder/timestamp slop.
    samples = [
        int.from_bytes(frames[i : i + 2], "little", signed=True) for i in range(0, len(frames), 2)
    ]
    first_signal = next(i for i, sample in enumerate(samples) if abs(sample) > 200)
    assert first_signal / 16_000 >= 0.60


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are required for the synthetic media conformance fixture",
)
async def test_video_without_audio_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "silent.mp4"
    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=0.25",
            "-c:v",
            "libx264",
            "-an",
            "-y",
            str(source),
        ],
        check=True,
    )
    with pytest.raises(MediaProcessingError, match="audio stream"):
        await probe_media(
            source,
            mime_type="video/mp4",
            ffprobe_path="ffprobe",
            max_duration_ms=8 * 60 * 60 * 1000,
        )


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are required for the synthetic media conformance fixture",
)
async def test_declared_container_mismatch_fails_closed(tmp_path: Path) -> None:
    wav = tmp_path / "actually-wave.bin"
    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.25",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            "-y",
            str(wav),
        ],
        check=True,
    )
    with pytest.raises(MediaProcessingError, match="container"):
        await probe_media(
            wav,
            mime_type="video/mp4",
            ffprobe_path="ffprobe",
            max_duration_ms=8 * 60 * 60 * 1000,
        )


def test_worker_image_pins_both_ffmpeg_binaries() -> None:
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert "mwader/static-ffmpeg:7.1.1" in dockerfile
    assert "/ffmpeg /usr/local/bin/ffmpeg" in dockerfile
    assert "/ffprobe /usr/local/bin/ffprobe" in dockerfile
