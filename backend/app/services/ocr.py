"""Selective OCR orchestration with cache-before-dispatch and exact native merge."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable

from app.core.config import Settings
from app.domain.canonical import CanonicalDocument
from app.domain.ocr import OcrError, OcrLedger, OcrPage, OcrProvider
from app.ingestion.native import merge_ocr


class OcrService:
    def __init__(self, provider: OcrProvider, ledger: OcrLedger, *, concurrency: int = 1) -> None:
        self._provider = provider
        self._ledger = ledger
        self._slots = asyncio.Semaphore(concurrency)

    async def run(
        self, document: CanonicalDocument, prepare: Callable[[int], bytes]
    ) -> tuple[CanonicalDocument, str | None]:
        pages = json.loads(document.generation_json).get("diagnostics", {}).get("pages", [])
        selected = [p["number"] for p in pages if p["outcome"] == "needs_ocr"]
        if not selected:
            return document, None
        policy = await self._ledger.policy()
        if not policy.enabled:
            return document, "ocr_disabled"
        policy.validate()
        converted = []
        reason = None
        # Bounded, sequential preprocessing; provider slots and durable tenant slots
        # include dispatch/settlement. Never materialize all PDFs in the parent.
        for page in selected:
            try:
                pdf = prepare(page)
                digest = hashlib.sha256(pdf).hexdigest()
                result = await self._ledger.cached(digest, self._provider.engine_id)
                if result is None:
                    async with self._slots:
                        await self._ledger.begin(digest, self._provider.engine_id, page, policy)
                        try:
                            result = await self._provider.recognize(OcrPage(page, pdf))
                        except OcrError as exc:
                            await self._ledger.finish(
                                digest, self._provider.engine_id, None, exc.code
                            )
                            raise
                        # Unexpected interruption leaves pending durable intent; no repeat charge.
                        await self._ledger.finish(digest, self._provider.engine_id, result, None)
                converted.append(
                    {
                        "page": page,
                        "text": result.text,
                        "engine_id": result.engine_id,
                        "confidence": result.confidence,
                    }
                )
            except OcrError as exc:
                reason = exc.code
                break
        return merge_ocr(document, converted) if converted else document, reason


def ocr_identity(settings: Settings, policy: object) -> dict[str, object]:
    return {
        "enabled": settings.ocr_enabled,
        "configured": bool(settings.openrouter_api_key.strip()),
        "model": settings.ocr_model,
        "engine": "openrouter:mistral-ocr:v1",
        "page_bytes": settings.ocr_max_page_bytes,
        "response_bytes": settings.ocr_max_response_bytes,
        "policy": str(policy),
        "version": 1,
    }
