"""Generated offline coverage and process containment for #722."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.eval.docintel.pdf_fixtures import document, text
from tests.eval.docintel.pdfium_fixtures import annotations, cid, incremental, object_stream


@pytest.fixture(autouse=True)
def require_pdfium() -> None:
    if sys.platform == "darwin":
        pytest.skip("macOS native extraction fails closed: enforceable memory cap unavailable")
    pytest.importorskip("lumen_docintel")
    from app.ingestion.native import NativeUnavailableError, _pdfium_library

    try:
        _pdfium_library()
    except NativeUnavailableError:
        pytest.skip("verified PDFium binary not installed")


def test_pdfium_generated_text_and_exact_spans() -> None:
    from app.ingestion.native import PdfiumExecutor

    executor = PdfiumExecutor()
    result = executor.extract_pdf(document([text(40, 700, 12, "Worker text")]))
    assert "Worker text" in result.rendered_text
    assert json.loads(result.generation_json)["parser_id"] == "rust-pdfium"
    for block, span in zip(result.blocks, result.spans, strict=True):
        assert result.rendered_text[span.char_start : span.char_end] == block.text
        assert block.regions[0].bbox is not None


def test_pdfium_scan_is_typed_needs_ocr() -> None:
    from app.ingestion.native import PdfiumExecutor

    result = PdfiumExecutor().extract_pdf(document([b""]))
    assert json.loads(result.generation_json)["outcome"] == "needs_ocr"


def test_pdfium_malformed_is_contained_then_next_document_works() -> None:
    from app.ingestion.native import PdfiumExecutor, PdfWorkerError

    executor = PdfiumExecutor()
    with pytest.raises(PdfWorkerError) as failed:
        executor.extract_pdf(b"%PDF-1.7\nmalformed")
    assert failed.value.code == "parse_error"
    assert "Healthy" in executor.extract_pdf(document([text(40, 700, 12, "Healthy")])).rendered_text


@pytest.mark.parametrize(
    "data,expected",
    [
        (object_stream(), "Object stream text"),
        (incremental(), "Latest revision"),
        (cid("CID text"), "CID text"),
        (cid("漢字中文"), "漢字中文"),
        (cid("שלום"), "שלום"),
        (cid("A😀B"), "A😀B"),
        (cid("漢字", rotation=90), "漢字"),
    ],
    ids=["object-xref-stream", "incremental", "cid", "cjk", "rtl", "supplementary", "rotated-cjk"],
)
def test_modern_constructs_and_unicode(data: bytes, expected: str) -> None:
    from app.ingestion.native import PdfiumExecutor

    result = PdfiumExecutor().extract_pdf(data)
    assert expected in result.rendered_text
    assert "Superseded" not in result.rendered_text
    for block, span in zip(result.blocks, result.spans, strict=True):
        assert result.rendered_text[span.char_start : span.char_end] == block.text


def test_unmapped_glyphs_and_mixed_pages_never_empty_success() -> None:
    from app.ingestion.native import PdfiumExecutor

    executor = PdfiumExecutor()
    assert (
        json.loads(executor.extract_pdf(cid("abc", unmapped=True)).generation_json)["outcome"]
        == "needs_ocr"
    )
    mixed = executor.extract_pdf(document([text(40, 700, 12, "Digital"), b""]))
    assert json.loads(mixed.generation_json)["outcome"] == "partial"
    assert len(mixed.source_parts) == 2


def test_encryption_and_annotation_policy() -> None:
    from pypdf import PdfReader, PdfWriter

    from app.ingestion.native import PdfiumExecutor, PdfWorkerError

    executor = PdfiumExecutor()
    result = executor.extract_pdf(annotations())
    assert "Page evidence" in result.rendered_text
    assert "Excluded" not in result.rendered_text
    for password in ("", "secret"):
        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(document([text(40, 700, 12, "Encrypted")]))))
        writer.encrypt(password)
        output = io.BytesIO()
        writer.write(output)
        with pytest.raises(PdfWorkerError) as failure:
            executor.extract_pdf(output.getvalue())
        assert failure.value.code == "encrypted"


def _replace_worker(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    from app.ingestion import _pdf_pool

    original = subprocess.Popen

    def replacement(command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        script = (
            "import sys; from app.ingestion._pdf_worker import apply_memory_limit; "
            "apply_memory_limit(int(sys.argv[1])); "
            "sys.stdout.buffer.write(b'R'); sys.stdout.buffer.flush(); " + body
        )
        return original([sys.executable, "-c", script, command[-1]], **kwargs)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(_pdf_pool.subprocess, "Popen", replacement)


def test_hard_hang_deadline_and_crash_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.domain.native_runtime import RuntimeBudget
    from app.ingestion.native import PdfiumExecutor, PdfWorkerError

    executor = PdfiumExecutor(workers=1)
    with monkeypatch.context() as patch:
        _replace_worker(patch, "import time; time.sleep(60)")
        with pytest.raises(PdfWorkerError, match="timed_out"):
            executor.extract_pdf(b"%PDF-1.7", budget=RuntimeBudget(timeout_ms=500))
    with monkeypatch.context() as patch:
        _replace_worker(patch, "import os; os._exit(42)")
        with pytest.raises(PdfWorkerError, match="worker_crashed"):
            executor.extract_pdf(b"%PDF-1.7")
    assert (
        "Replaced" in executor.extract_pdf(document([text(40, 700, 12, "Replaced")])).rendered_text
    )


def test_cancellation_terminates_non_cooperative_child(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from app.ingestion import _pdf_pool
    from app.ingestion.native import CancellationHandle, PdfiumExecutor, PdfWorkerError

    executor = PdfiumExecutor(workers=1)
    token = CancellationHandle()
    _replace_worker(monkeypatch, "import time; time.sleep(60)")
    entered = threading.Event()
    original_read = _pdf_pool.read_exact

    def observed_read(stream: object, size: int) -> bytes:
        value = original_read(stream, size)  # type: ignore[arg-type]
        if value == b"R":
            entered.set()
        return value

    monkeypatch.setattr(_pdf_pool, "read_exact", observed_read)
    with ThreadPoolExecutor(max_workers=1) as threads:
        future = threads.submit(executor.extract_pdf, b"%PDF-1.7", cancellation=token)
        assert entered.wait(timeout=5)
        token.cancel()
        with pytest.raises(PdfWorkerError, match="cancelled"):
            future.result(timeout=5)


def test_pool_memory_configuration_and_os_memory_limit() -> None:
    from app.ingestion._pdf_pool import PdfProcessPool

    with pytest.raises(ValueError):
        PdfProcessPool(workers=3, memory_cap_bytes=100, total_memory_bytes=200)
    assert PdfProcessPool(memory_cap_bytes=100, total_memory_bytes=100).workers == 1
    script = (
        "from app.ingestion._pdf_worker import apply_memory_limit; "
        "apply_memory_limit(96*1024*1024); blocks=[]\n"
        "try:\n for _ in range(24): blocks.append(bytearray(8*1024*1024))\n"
        "except MemoryError:\n print('limited')"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=10)
    assert b"limited" in result.stdout


def test_queued_document_has_deadline_and_cancellation() -> None:
    from app.domain.native_runtime import RuntimeBudget
    from app.ingestion.native import CancellationHandle, PdfiumExecutor, PdfWorkerError

    executor = PdfiumExecutor(workers=1)
    assert executor._pool._slots.acquire(blocking=False)
    try:
        with pytest.raises(PdfWorkerError, match="timed_out"):
            executor.extract_pdf(b"%PDF-1.7", budget=RuntimeBudget(timeout_ms=50))
        token = CancellationHandle()
        token.cancel()
        with pytest.raises(PdfWorkerError, match="cancelled"):
            executor.extract_pdf(b"%PDF-1.7", cancellation=token)
    finally:
        executor._pool._slots.release()


def test_pdfium_accounting_limits_are_typed_and_shadow_keeps_python() -> None:
    from app.domain.native_runtime import RuntimeBudget
    from app.ingestion.native import PdfiumExecutor, PdfWorkerError, parse_pdf_candidate

    data = document([text(40, 700, 12, "Evidence")])
    budget = RuntimeBudget(max_memory_bytes=128)
    with pytest.raises(PdfWorkerError, match="budget"):
        PdfiumExecutor().extract_pdf(data, budget=budget)
    shadow = parse_pdf_candidate(data, mode="shadow", budget=budget)
    assert shadow.text == "Evidence"
    assert shadow.native_error == "native_failed"


def test_hostile_recursive_form_is_bounded_and_never_empty_success() -> None:
    from app.domain.native_runtime import RuntimeBudget
    from app.ingestion.native import PdfiumExecutor, PdfWorkerError
    from tests.eval.docintel.pdf_fixtures import objects

    data = objects(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /XObject << /Loop 5 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length 8 >>\nstream\n/Loop Do\nendstream",
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 612 792] "
            b"/Resources << /XObject << /Loop 5 0 R >> >> /Length 8 >>\n"
            b"stream\n/Loop Do\nendstream",
        ]
    )
    try:
        result = PdfiumExecutor().extract_pdf(data, budget=RuntimeBudget(timeout_ms=1000))
    except PdfWorkerError as error:
        assert error.code in {"budget", "parse_error", "timed_out", "worker_crashed"}
    else:
        assert json.loads(result.generation_json)["outcome"] == "needs_ocr"
