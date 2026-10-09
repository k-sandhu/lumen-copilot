"""OpenRouter file-parser HTTP boundary. The assistant answer is never evidence."""

from __future__ import annotations

import base64
import json
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.config import Settings
from app.domain.ocr import OcrError, OcrPage, OcrResult

_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterOcrProvider:
    engine_id = "openrouter:mistral-ocr:v1"

    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = http_client

    async def recognize(self, page: OcrPage) -> OcrResult:
        settings = self._settings
        if not settings.ocr_enabled:
            raise OcrError("ocr_disabled")
        if not settings.openrouter_api_key.strip() or not settings.ocr_model.strip():
            raise OcrError("ocr_unconfigured")
        if (
            page.number < 1
            or not page.pdf.startswith(b"%PDF-")
            or len(page.pdf) > settings.ocr_max_page_bytes
        ):
            raise OcrError("ocr_invalid_input")
        payload = {
            "model": settings.ocr_model,
            "max_tokens": 1,
            "usage": {"include": True},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Reply OK."},
                        {
                            "type": "file",
                            "file": {
                                "filename": f"page-{page.number:04d}.pdf",
                                "file_data": "data:application/pdf;base64,"
                                + base64.b64encode(page.pdf).decode("ascii"),
                            },
                        },
                    ],
                }
            ],
            "plugins": [{"id": "file-parser", "pdf": {"engine": "mistral-ocr"}}],
            "provider": {"allow_fallbacks": False, "data_collection": "deny"},
        }
        client = self._client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        try:
            async with client.stream(
                "POST",
                _ENDPOINT,
                json=payload,
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
                timeout=settings.ocr_timeout_seconds,
                follow_redirects=False,
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > settings.ocr_max_response_bytes:
                        raise OcrError("ocr_response_budget")
                    body.extend(chunk)
                raw: Any = json.loads(body)
                if not isinstance(raw, dict):
                    raise OcrError("ocr_malformed_response")
                annotations = []
                choices = raw.get("choices", [])
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    message = choices[0].get("message", {})
                    if isinstance(message, dict):
                        annotations.extend(message.get("annotations", []))
                error = raw.get("error", {})
                if isinstance(error, dict) and isinstance(error.get("metadata"), dict):
                    annotations.extend(error["metadata"].get("file_annotations", []))
                texts: dict[str, str] = {}
                for annotation in annotations:
                    if not isinstance(annotation, dict) or annotation.get("type") != "file":
                        continue
                    file = annotation.get("file")
                    if not isinstance(file, dict) or not isinstance(file.get("hash"), str):
                        raise OcrError("ocr_malformed_response")
                    parts = file.get("content")
                    if not isinstance(parts, list):
                        raise OcrError("ocr_malformed_response")
                    text_parts = []
                    for part in parts:
                        if not isinstance(part, dict):
                            raise OcrError("ocr_malformed_response")
                        if part.get("type") == "text":
                            if not isinstance(part.get("text"), str):
                                raise OcrError("ocr_malformed_response")
                            text_parts.append(part["text"])
                    text = "".join(text_parts)
                    if file["hash"] in texts and texts[file["hash"]] != text:
                        raise OcrError("ocr_malformed_response")
                    texts[file["hash"]] = text
                if len(texts) > 1:
                    raise OcrError("ocr_malformed_response")
                text = next(iter(texts.values()), "")
                if not text.strip():
                    raise OcrError(
                        "ocr_provider_rejected"
                        if response.status_code != 200
                        else "ocr_missing_text"
                    )
                usage = raw.get("usage", {})
                cost = None
                if isinstance(usage, dict) and "cost" in usage:
                    if isinstance(usage["cost"], bool):
                        raise OcrError("ocr_malformed_response")
                    cost = Decimal(str(usage["cost"]))
                    if not cost.is_finite() or cost < 0:
                        raise OcrError("ocr_malformed_response")

                def tokens(name: str) -> int | None:
                    value = usage.get(name) if isinstance(usage, dict) else None
                    if value is not None and (
                        isinstance(value, bool) or not isinstance(value, int) or value < 0
                    ):
                        raise OcrError("ocr_malformed_response")
                    return value

                return OcrResult(
                    text,
                    self.engine_id,
                    None,
                    cost,
                    tokens("prompt_tokens"),
                    tokens("completion_tokens"),
                )
        except httpx.TimeoutException:
            raise OcrError("ocr_timeout") from None
        except httpx.TransportError:
            raise OcrError("ocr_transport") from None
        except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation):
            raise OcrError("ocr_malformed_response") from None
        finally:
            if self._client is None:
                await client.aclose()
