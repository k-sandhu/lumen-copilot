"""Narrow OpenRouter speech-to-text adapter (ADR-0023 §3).

This is the sole direct provider HTTP exception to the LiteLLM transport rule.
It stays inside ``app.llm`` and returns only provider-neutral domain objects.
The response is validated fail-closed: no ordered words, timestamps, or speaker
labels means no transcript and therefore no unverifiable media citation.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.errors import DependencyError
from app.domain.llm import Transcription, TranscriptionWord


class InvalidTranscriptionResponse(Exception):
    """The provider answered, but did not supply valid citable evidence."""


def _endpoint(base_url: str) -> str:
    parts = urlsplit(base_url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or (host != "openrouter.ai" and not host.endswith(".openrouter.ai")):
        raise ValueError("TRANSCRIPTION_BASE_URL must be an HTTPS OpenRouter endpoint")
    path = f"{parts.path.rstrip('/')}/audio/transcriptions"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _xai_options(options: dict[str, object], *, require_diarization: bool) -> dict[str, object]:
    """Validate the configured options nested at ``provider.options.xai``.

    OpenRouter's canonical STT JSON shape routes provider-specific passthrough
    here.  In particular xAI diarization is *not* an OpenAI
    ``verbose_json``/``timestamp_granularities`` form option; those are rejected
    on non-OpenAI-compatible upstreams.  JSON round-tripping also rejects custom
    Python objects before they can reach the provider.
    """
    merged = dict(options)
    if require_diarization:
        merged["diarize"] = True
    if any(not isinstance(key, str) or not key for key in merged):
        raise ValueError("transcription provider option names must be non-empty strings")
    try:
        normalized = json.loads(json.dumps(merged, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("transcription provider options must be finite JSON values") from exc
    if not isinstance(normalized, dict):  # pragma: no cover - json object by construction
        raise ValueError("transcription provider options must be an object")
    return normalized


def _number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidTranscriptionResponse(f"transcription word {field} is not numeric")
    number = float(value)
    if number < 0 or number != number or number in {float("inf"), float("-inf")}:
        raise InvalidTranscriptionResponse(f"transcription word {field} is invalid")
    return number


def _parse_response(payload: object, *, model: str) -> Transcription:
    if not isinstance(payload, dict):
        raise InvalidTranscriptionResponse("transcription response was not an object")
    raw_text = payload.get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise InvalidTranscriptionResponse("transcription response had no text")
    raw_words = payload.get("words")
    if not isinstance(raw_words, list) or not raw_words:
        raise InvalidTranscriptionResponse("transcription response had no timestamped words")

    words: list[TranscriptionWord] = []
    previous_start = -1
    for raw in raw_words:
        if not isinstance(raw, dict):
            raise InvalidTranscriptionResponse("transcription word was not an object")
        raw_word = raw.get("text", raw.get("word"))
        if not isinstance(raw_word, str) or not raw_word.strip():
            raise InvalidTranscriptionResponse("transcription word had no text")
        raw_speaker = raw.get("speaker")
        if isinstance(raw_speaker, bool) or not isinstance(raw_speaker, str | int):
            raise InvalidTranscriptionResponse("transcription word had no speaker label")
        if isinstance(raw_speaker, int):
            if raw_speaker < 0:
                raise InvalidTranscriptionResponse("transcription word had no speaker label")
            speaker = f"speaker-{raw_speaker}"
        else:
            speaker = raw_speaker.strip()
            if not speaker:
                raise InvalidTranscriptionResponse("transcription word had no speaker label")
        start_ms = round(_number(raw.get("start"), field="start") * 1000)
        end_ms = round(_number(raw.get("end"), field="end") * 1000)
        if end_ms <= start_ms:
            raise InvalidTranscriptionResponse("transcription word has an empty/reversed span")
        if start_ms < previous_start:
            raise InvalidTranscriptionResponse("transcription words are not ordered")
        previous_start = start_ms

        raw_confidence = raw.get("confidence")
        confidence: float | None = None
        if raw_confidence is not None:
            confidence = _number(raw_confidence, field="confidence")
            if confidence > 1:
                raise InvalidTranscriptionResponse("transcription confidence is outside 0..1")
        words.append(
            TranscriptionWord(
                text=raw_word.strip(),
                start_ms=start_ms,
                end_ms=end_ms,
                speaker_label=speaker,
                confidence=confidence,
            )
        )

    language = payload.get("language")
    if language is not None and not isinstance(language, str):
        raise InvalidTranscriptionResponse("transcription language is invalid")
    normalized_language = language.strip() if isinstance(language, str) else None
    return Transcription(
        text=raw_text.strip(),
        words=tuple(words),
        # xAI currently uses an empty string when language detection is not
        # available. Unknown language is valid provenance; malformed types are
        # not. Normalize the provider sentinel to our nullable domain value.
        language=normalized_language or None,
        model=model,
    )


class OpenRouterTranscriber:
    """Async OpenRouter transcription client with typed, opaque error mapping."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float,
        provider_options: dict[str, object],
        require_diarization: bool,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._url = _endpoint(base_url)
        self._model = model
        self._timeout = timeout_seconds
        self._xai_options = _xai_options(provider_options, require_diarization=require_diarization)
        self._client = http_client

    async def transcribe(self, audio_path: Path) -> Transcription:
        if not self._api_key:
            raise DependencyError(
                "Speech transcription is not configured.", code="transcription_unconfigured"
            )
        if not audio_path.is_file():
            raise InvalidTranscriptionResponse("transcription audio chunk does not exist")

        # Chunks are bounded to <=10 minutes by ingestion. Reading one chunk is
        # bounded (~19 MiB for mono PCM16/16 kHz) and is required for
        # OpenRouter's canonical base64-in-JSON ``input_audio`` request shape.
        audio = await asyncio.to_thread(audio_path.read_bytes)
        payload = {
            "model": self._model,
            "input_audio": {
                "data": base64.b64encode(audio).decode("ascii"),
                "format": "wav",
            },
            "provider": {"options": {"xai": self._xai_options}},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(follow_redirects=False)
        try:
            response = await client.post(
                self._url,
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise DependencyError(
                "Speech transcription timed out.", code="transcription_timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise DependencyError(
                "Speech transcription is unavailable.", code="transcription_unavailable"
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code == 429:
            raise DependencyError(
                "Speech transcription is rate limited.", code="transcription_rate_limited"
            )
        if response.status_code in {401, 403}:
            raise DependencyError(
                "Speech transcription credentials were rejected.",
                code="transcription_auth_failed",
            )
        if response.status_code >= 500:
            raise DependencyError(
                "Speech transcription is unavailable.", code="transcription_unavailable"
            )
        if response.status_code >= 400:
            raise InvalidTranscriptionResponse(
                f"transcription provider rejected the audio ({response.status_code})"
            )
        try:
            response_payload: Any = response.json()
        except ValueError as exc:
            raise InvalidTranscriptionResponse("transcription response was not JSON") from exc
        return _parse_response(response_payload, model=self._model)


__all__ = ["InvalidTranscriptionResponse", "OpenRouterTranscriber"]
