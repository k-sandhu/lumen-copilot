"""Generated-only OCR preprocessing and canonical evidence, one isolated worker."""

import asyncio
import io
import json
import struct
import sys
import zlib
from decimal import Decimal
from uuid import uuid4

import pytest
from pypdf import PdfReader

from app.domain.native_runtime import RuntimeBudget
from app.domain.ocr import OcrPolicy, OcrResult
from app.ingestion import native
from app.services.ocr import OcrService
from tests import test_ingestion_task as fixtures
from tests.eval.docintel.pdfium_fixtures import document, text

sqlite_engine = fixtures.sqlite_engine
_offline_index_store = fixtures._offline_index_store

pytestmark = pytest.mark.skipif(
    not native.native_available() or sys.platform not in {"win32", "linux"},
    reason="optional native extension unavailable",
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
    # Cross a clock second to exercise fresh PDFium wrapper CreationDate as well.
    await asyncio.sleep(1.1)
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


async def test_preprocessing_budget_is_typed_and_never_dispatches():
    canonical = native.image_canonical(png())
    provider = Provider()
    ledger = Ledger()

    def rejected(page):
        raise native.PdfWorkerError("budget")

    result, reason = await OcrService(provider, ledger).run(canonical, rejected)
    assert reason == "ocr_preprocess_budget"
    assert result.rendered_text == "" and provider.pages == [] and ledger.starts == 0


async def test_unexpected_provider_cost_is_recorded_but_not_published():
    canonical = native.image_canonical(png())

    class ExpensiveProvider(Provider):
        async def recognize(self, page):
            return OcrResult("generated text", self.engine_id, cost_usd=Decimal("0.03"))

    ledger = Ledger()
    result, reason = await OcrService(ExpensiveProvider(), ledger).run(
        canonical, lambda page: b"%PDF-generated"
    )
    assert reason == "ocr_cost_exceeded" and result.rendered_text == ""
    assert ledger.starts == 1 and next(iter(ledger.values.values())).cost_usd == Decimal("0.03")


async def test_enabled_ingestion_resumes_after_downstream_fault_without_repayment(
    sqlite_engine, _offline_index_store, monkeypatch
):
    from sqlalchemy import select

    from app.db import models
    from app.db.ingestion_stages import StageRepository
    from app.db.ocr import OcrRepository
    from app.db.session import tenant_session_scope
    from app.domain.entities import DocumentStatus
    from app.tasks.ingest import IngestionError, ingest_document_async

    tenant, doc = await fixtures._seed_document(mime_type="image/png", key="scan")
    async with tenant_session_scope(tenant) as session:
        admin = (
            await session.execute(select(models.User).where(models.User.tenant_id == tenant))
        ).scalar_one()
        admin.roles = ["admin"]
        await OcrRepository(session, tenant).configure(
            OcrPolicy(tenant, True, 2, Decimal("0.02"), Decimal("0.01"), 1, admin.id)
        )
    provider = Provider()
    monkeypatch.setattr("app.tasks.ingest.OpenRouterOcrProvider", lambda settings: provider)
    executor = native.PdfiumExecutor(workers=1)
    monkeypatch.setattr("app.tasks.ingest._default_pdf_executor", lambda pid: (executor, BUDGET))
    store = fixtures._FakeObjectStore()
    store.put(str(tenant), "scan", png())
    settings = fixtures._settings(
        ocr_enabled=True, ocr_model="fixture-model", OPENROUTER_API_KEY="synthetic"
    )
    with pytest.raises(IngestionError):
        await ingest_document_async(
            tenant,
            doc,
            settings=settings,
            object_store=store,
            gateway=fixtures._FakeGateway(fail=True),
        )
    assert provider.pages == [1]
    result = await ingest_document_async(
        tenant, doc, settings=settings, object_store=store, gateway=fixtures._FakeGateway()
    )
    assert result.status is DocumentStatus.READY and provider.pages == [1]
    async with tenant_session_scope(tenant) as session:
        chunks = (
            (
                await session.execute(
                    select(models.Chunk).where(
                        models.Chunk.tenant_id == tenant, models.Chunk.document_id == doc
                    )
                )
            )
            .scalars()
            .all()
        )
        assert chunks and all(c.machine_read for c in chunks)
        stages = await StageRepository(session, tenant).list(doc)
        ocr = next(s for s in stages if s.stage == "ocr")
        assert json.loads(ocr.payload_json)["machine_read_spans"]
        policy = (await session.execute(select(models.OcrTenantPolicy))).scalar_one()
        assert policy.pages_used == 1
