"""Generated scanned inputs never become successful empty documents."""

import json

import pytest

from app.db.ingestion_stages import StageRepository
from app.db.repositories import DocumentRepository
from app.db.session import tenant_session_scope
from app.domain.entities import DocumentStatus
from app.tasks.ingest import ingest_document_async
from tests import test_ingestion_task as fixtures

sqlite_engine = fixtures.sqlite_engine
_offline_index_store = fixtures._offline_index_store


@pytest.mark.parametrize(
    "mime,data", [("application/pdf", None), ("image/png", b"generated image fixture")]
)
async def test_disabled_ocr_has_visible_typed_outcome(
    sqlite_engine, _offline_index_store, mime, data
):
    if data is None:
        import io

        from pypdf import PdfWriter

        out = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(80, 80)
        writer.write(out)
        data = out.getvalue()
    tenant, doc = await fixtures._seed_document(mime_type=mime, key="scan")
    store = fixtures._FakeObjectStore()
    store.put(str(tenant), "scan", data)
    gateway = fixtures._FakeGateway()
    result = await ingest_document_async(
        tenant, doc, settings=fixtures._settings(), object_store=store, gateway=gateway
    )
    assert result.status is DocumentStatus.FAILED
    assert gateway.calls == []
    async with tenant_session_scope(tenant) as session:
        document = await DocumentRepository(session, tenant).get(doc)
        assert document.ingestion_failure["code"] == "needs_ocr"
        stages = await StageRepository(session, tenant).list(doc)
        assert [s.stage for s in stages] == ["detect", "extract", "ocr"]
        assert json.loads(stages[-1].payload_json)["ocr_reason"] == "ocr_disabled"


@pytest.mark.parametrize("kind", ["budget", "unavailable"])
def test_native_extraction_failure_keeps_typed_ocr_outcome(monkeypatch, kind):
    from app.ingestion import native

    monkeypatch.setattr(native, "native_available", lambda: True)

    def rejected(pid):
        if kind == "budget":
            raise native.PdfWorkerError("budget")
        raise native.NativeUnavailableError("binary unavailable")

    monkeypatch.setattr(native, "_default_pdf_executor", rejected)
    result = native.extract_ocr_candidate(
        b"%PDF-generated", mime_type="application/pdf", enabled=True
    )
    assert result["needs_ocr"] is True and result["canonical_json"] is None
    assert result["ocr_reason"] == (
        "ocr_extract_budget" if kind == "budget" else "ocr_native_unavailable"
    )
