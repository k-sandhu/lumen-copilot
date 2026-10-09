"""Generated-only OCR preprocessing and canonical evidence, one isolated worker."""

import io
import json
import struct
import zlib
from decimal import Decimal
from uuid import uuid4

import pytest
from pypdf import PdfReader

from app.domain.native_runtime import RuntimeBudget
from app.domain.ocr import OcrPolicy, OcrResult
from app.ingestion import native
from app.services.ocr import OcrService
from tests.eval.docintel.pdfium_fixtures import document, text

pytestmark = pytest.mark.skipif(
    not native.native_available(), reason="optional native extension unavailable"
)
BUDGET = RuntimeBudget(
    max_work_units=5_000_000, max_memory_bytes=256 * 1024 * 1024, max_output_chars=2_000_000
)


class Ledger:
    tenant_id = uuid4()

    def __init__(self):
        self.values = {}
        self.starts = 0

    async def policy(self):
        return OcrPolicy(self.tenant_id, True, 10, Decimal("1"), Decimal("0.01"), 1, uuid4())

    async def cached(self, digest, engine):
        return self.values.get(digest)

    async def begin(self, digest, engine, page, policy):
        self.starts += 1

    async def finish(self, digest, engine, result, error):
        self.values[digest] = result


class Provider:
    engine_id = "fixture"

    def __init__(self):
        self.pages = []

    async def recognize(self, page):
        self.pages.append(page.number)
        assert len(PdfReader(io.BytesIO(page.pdf)).pages) == 1
        return OcrResult("OCR café 😀 e\u0301", self.engine_id)


async def test_only_scanned_page_is_split_and_paid_once():
    data = document([text(40, 700, 12, "Digital control"), b"", text(40, 700, 12, "More digital")])
    executor = native.PdfiumExecutor(workers=1)
    canonical = executor.extract_pdf(data, budget=BUDGET)
    provider = Provider()
    ledger = Ledger()
    service = OcrService(provider, ledger)

    def prepare(page):
        return executor.prepare_ocr_page(data, page=page, budget=BUDGET)

    result, reason = await service.run(canonical, prepare)
    assert reason is None and provider.pages == [2]
    assert json.loads(result.generation_json)["outcome"] == "indexed"
    assert result.source_parts[1].number == 2
    assert [block.text for block in result.blocks if block.origin != "ocr"] == [
        block.text for block in canonical.blocks
    ]
    assert result.blocks[1].regions[0].bbox is None
    assert all(
        result.rendered_text[s.char_start : s.char_end] == b.text
        for s, b in zip(result.spans, result.blocks, strict=True)
    )
    # Re-extract/split the same source for a new ingestion attempt: stable hash and cache hit.
    repeated = native.PdfiumExecutor(workers=1).prepare_ocr_page(data, page=2, budget=BUDGET)
    assert repeated == prepare(2)
    again, _ = await service.run(canonical, prepare)
    assert again.rendered_text == result.rendered_text and ledger.starts == 1


def png():
    def chunk(kind, data):
        return (
            struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(
            b"IDAT", zlib.compress(b"\x00" + b"\xff\xff\xff" * 2 + b"\x00" + b"\x00\x00\x00" * 2)
        )
        + chunk(b"IEND", b"")
    )


def test_standalone_png_is_one_page_and_hostile_input_is_contained():
    executor = native.PdfiumExecutor(workers=1)
    data = executor.prepare_ocr_page(png(), page=1, image=True, budget=BUDGET)
    assert len(PdfReader(io.BytesIO(data)).pages) == 1
    with pytest.raises(native.PdfWorkerError):
        executor.prepare_ocr_page(b"corrupt", page=1, image=True, budget=BUDGET)
    assert executor.prepare_ocr_page(png(), page=1, image=True, budget=BUDGET).startswith(b"%PDF-")
