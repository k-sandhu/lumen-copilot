"""OpenRouter speech-to-text adapter contract (spec 0008 / ADR-0023).

Offline tests pin the provider boundary without opening a socket.  The live
conformance test is opt-in because it incurs provider cost; it is the only test
allowed to establish that a configured route really returns word timestamps and
speaker labels.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import DependencyError
from app.llm import LLMGateway
from app.llm.openrouter_stt import InvalidTranscriptionResponse, OpenRouterTranscriber


def _audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "chunk.wav"
    path.write_bytes(b"RIFF offline fixture")
    return path


@pytest.mark.parametrize(
    "base_url",
    ["http://openrouter.ai/api/v1", "https://localhost/api/v1", "https://example.com/v1"],
)
def test_adapter_rejects_non_openrouter_or_non_tls_base_urls(base_url: str) -> None:
    with pytest.raises(ValueError, match="OpenRouter"):
        OpenRouterTranscriber(
            api_key="test",
            base_url=base_url,
            model="x-ai/grok-stt-1.0",
            timeout_seconds=30,
            provider_options={"diarize": True},
            require_diarization=True,
        )


async def test_maps_diarized_words_to_provider_neutral_milliseconds(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["content_type"] = request.headers.get("content-type")
        seen["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "text": "Hello John",
                "language": "en",
                "words": [
                    {
                        "text": "Hello",
                        "start": 0.0,
                        "end": 0.42,
                        "speaker": 0,
                        "confidence": 0.98,
                    },
                    {
                        "text": "John",
                        "start": 0.44,
                        "end": 0.81,
                        "speaker": 1,
                    },
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        adapter = OpenRouterTranscriber(
            api_key="secret-for-test",
            base_url="https://openrouter.ai/api/v1/",
            model="x-ai/grok-stt-1.0",
            timeout_seconds=30,
            provider_options={
                "diarize": True,
                "language": "en",
            },
            require_diarization=True,
            http_client=client,
        )
        result = await adapter.transcribe(_audio_file(tmp_path))

    assert seen["url"] == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert seen["authorization"] == "Bearer secret-for-test"
    assert seen["content_type"] == "application/json"
    payload = seen["json"]
    assert isinstance(payload, dict)
    assert payload == {
        "model": "x-ai/grok-stt-1.0",
        "input_audio": {
            "data": base64.b64encode(b"RIFF offline fixture").decode("ascii"),
            "format": "wav",
        },
        "provider": {"options": {"xai": {"diarize": True, "language": "en"}}},
    }
    assert result.text == "Hello John"
    assert result.language == "en"
    assert result.model == "x-ai/grok-stt-1.0"
    assert [(w.start_ms, w.end_ms, w.speaker_label) for w in result.words] == [
        (0, 420, "speaker-0"),
        (440, 810, "speaker-1"),
    ]
    assert result.words[0].confidence == pytest.approx(0.98)


async def test_blank_provider_language_is_normalized_to_unknown(tmp_path: Path) -> None:
    """xAI currently returns an empty language field when detection is unavailable."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "text": "Hello",
                "language": "",
                "words": [{"text": "Hello", "start": 0.0, "end": 0.2, "speaker": 0}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        adapter = OpenRouterTranscriber(
            api_key="test",
            base_url="https://openrouter.ai/api/v1",
            model="x-ai/grok-stt-1.0",
            timeout_seconds=30,
            provider_options={},
            require_diarization=True,
            http_client=client,
        )
        result = await adapter.transcribe(_audio_file(tmp_path))

    assert result.language is None


@pytest.mark.parametrize(
    "words",
    [
        [{"text": "missing speaker", "start": 0.0, "end": 0.2}],
        [{"text": "negative speaker", "start": 0.0, "end": 0.2, "speaker": -1}],
        [{"text": "boolean speaker", "start": 0.0, "end": 0.2, "speaker": True}],
        [{"text": "float speaker", "start": 0.0, "end": 0.2, "speaker": 1.5}],
        [{"text": "blank speaker", "start": 0.0, "end": 0.2, "speaker": ""}],
        [{"text": "reversed", "start": 0.4, "end": 0.2, "speaker": "s0"}],
        [
            {"text": "later", "start": 1.0, "end": 1.2, "speaker": "s0"},
            {"text": "earlier", "start": 0.5, "end": 0.8, "speaker": "s0"},
        ],
    ],
)
async def test_fails_closed_when_word_evidence_is_not_citable(
    tmp_path: Path, words: list[dict[str, object]]
) -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "bad", "words": words})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        adapter = OpenRouterTranscriber(
            api_key="test",
            base_url="https://openrouter.ai/api/v1",
            model="x-ai/grok-stt-1.0",
            timeout_seconds=30,
            provider_options={},
            require_diarization=True,
            http_client=client,
        )
        with pytest.raises(InvalidTranscriptionResponse):
            await adapter.transcribe(_audio_file(tmp_path))


async def test_maps_provider_failure_without_leaking_response_body(tmp_path: Path) -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="provider-secret-debug-body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        adapter = OpenRouterTranscriber(
            api_key="test",
            base_url="https://openrouter.ai/api/v1",
            model="x-ai/grok-stt-1.0",
            timeout_seconds=30,
            provider_options={},
            require_diarization=True,
            http_client=client,
        )
        with pytest.raises(DependencyError) as caught:
            await adapter.transcribe(_audio_file(tmp_path))

    assert caught.value.code == "transcription_rate_limited"
    assert "provider-secret-debug-body" not in str(caught.value)


async def test_llm_gateway_is_the_provider_neutral_task_seam(tmp_path: Path) -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "text": "Hello",
                "language": "en",
                "words": [{"text": "Hello", "start": 0.0, "end": 0.2, "speaker": "s0"}],
            },
        )

    settings = Settings(
        OPENROUTER_API_KEY="test-key",
        TRANSCRIPTION_MODEL="x-ai/grok-stt-1.0",
        TRANSCRIPTION_BASE_URL="https://openrouter.ai/api/v1",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        result = await LLMGateway(settings).transcribe(_audio_file(tmp_path), http_client=client)
    assert result.words[0].speaker_label == "s0"


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("RUN_LIVE") != "1"
    or not os.getenv("OPENROUTER_API_KEY")
    or not os.getenv("OPENROUTER_STT_LIVE_FIXTURE"),
    reason=(
        "set RUN_LIVE=1 + OPENROUTER_API_KEY + OPENROUTER_STT_LIVE_FIXTURE " "to a short spoken WAV"
    ),
)
async def test_openrouter_route_live_returns_words_and_speakers(tmp_path: Path) -> None:
    """Cost-bearing proof of the configured route's real timestamp/diarization shape."""
    del tmp_path
    wav = Path(os.environ["OPENROUTER_STT_LIVE_FIXTURE"])
    assert wav.is_file()
    adapter = OpenRouterTranscriber(
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url=os.getenv("TRANSCRIPTION_BASE_URL", "https://openrouter.ai/api/v1"),
        model=os.getenv("TRANSCRIPTION_MODEL", "x-ai/grok-stt-1.0"),
        timeout_seconds=60,
        provider_options={"diarize": True},
        require_diarization=True,
    )
    result = await adapter.transcribe(wav)
    assert result.words
    assert all(word.end_ms > word.start_ms >= 0 for word in result.words)
    assert all(word.speaker_label for word in result.words)
