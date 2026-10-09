"""Optional native ingestion facade (ADR-0027); the sole extension importer."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from app.core.config import Settings

from app.domain.canonical import CanonicalDocument
from app.domain.document_detection import DetectedDocument
from app.domain.ingestion_shadow import CandidateExtraction
from app.domain.native_chunking import ChunkedDocument
from app.domain.native_normalization import NormalizedDocument
from app.domain.native_runtime import ComputedUnits, RuntimeBudget
from app.ingestion._pdf_pool import PdfProcessPool
from app.ingestion._pdf_pool import PdfWorkerError as PdfWorkerError


def _extension() -> ModuleType | None:
    try:
        return import_module("lumen_docintel")
    except ImportError:
        return None


def native_available() -> bool:
    """Availability is distinct from a format's approved cutover status."""
    return _extension() is not None


class NativeUnavailableError(Exception):
    """The optional computation extension has not been installed."""


def render_canonical(document_json: str) -> CanonicalDocument:
    """Validate/render an inert model; no format extraction or activation."""
    extension = _extension()
    if extension is None:
        raise NativeUnavailableError("native ingestion extension is unavailable")
    return CanonicalDocument.from_render_json(extension.render_document(document_json))


# Recognition does not land a parser. Format issue implementations register here.
_LANDED_NATIVE_FORMATS: frozenset[str] = frozenset()
_PYTHON_FALLBACK_FORMATS = frozenset({"pdf", "docx", "pptx", "xlsx", "text", "markdown"})


def detect_content(data: bytes, *, declared_mime: str | None = None) -> DetectedDocument:
    extension = _extension()
    if extension is None:
        raise NativeUnavailableError("native ingestion extension is unavailable")
    return DetectedDocument.from_json(extension.detect_format(data, declared_mime))


def plan_native_route(detected: DetectedDocument, *, enabled_formats: frozenset[str]) -> str:
    """Config can enable installed parsers; it cannot claim one has landed."""
    if detected.format in _LANDED_NATIVE_FORMATS & enabled_formats:
        return "native"
    if detected.format in _PYTHON_FALLBACK_FORMATS:
        return "python_fallback"
    return "unsupported"


class CancellationHandle:
    """Orchestration-owned cancellation; no vendor object leaves this facade."""

    def __init__(self) -> None:
        extension = _extension()
        if extension is None:
            raise NativeUnavailableError("native ingestion extension is unavailable")
        self._token = extension.CancellationToken()

    def cancel(self) -> None:
        self._token.cancel()

    def is_cancelled(self) -> bool:
        return bool(self._token.is_cancelled())


_DEFAULT_BUDGET = RuntimeBudget()


def _pdf_worker_extract(data: bytes, library: str, budget: dict[str, int], engine: str) -> str:
    """Private child-only native entry. OS limits precede this import."""
    if engine == "pypdf":
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise PdfWorkerError("encrypted")
        pages: list[str] = []
        count = 0
        for page in reader.pages:
            if len(pages) >= budget["max_work_units"]:
                raise MemoryError
            value = page.extract_text() or ""
            count += len(value)
            if count > budget["max_output_chars"]:
                raise MemoryError
            pages.append(value)
        return json.dumps(
            {
                "rendered_text": "\n\n".join(pages),
                "pages": len(pages),
                "outcome": "needs_ocr" if any(not p.strip() for p in pages) else "indexed",
            },
            ensure_ascii=False,
        )
    extension = _extension()
    if extension is None:
        raise NativeUnavailableError("native ingestion extension is unavailable")
    if engine == "pdfium":
        return str(extension._extract_pdfium_worker(data, library, json.dumps(budget)))
    if engine == "in_core":
        return str(
            extension.Runtime(1, 1).extract_pdf(
                data, json.dumps(budget), extension.CancellationToken()
            )
        )
    raise ValueError("invalid PDF engine")


def _pdfium_library() -> str:
    extension = _extension()
    if extension is None:
        raise NativeUnavailableError("native ingestion extension is unavailable")
    # Wheels carry their verified library alongside the extension. Editable builds
    # use the same downloader destination. No system-library search or latest URL.
    roots = [
        Path(str(extension.__file__)).parent / "pdfium",
        Path(__file__).resolve().parents[3]
        / "rust/crates/lumen-docintel-py/pdfium-wheel-data/platlib/lumen_docintel/pdfium",
    ]
    for root in roots:
        pin = root / "pin.json"
        if pin.is_file():
            manifest = json.loads(pin.read_text(encoding="utf-8"))
            if manifest["release"] == "chromium/7881":
                library = root / manifest["library"]
                if library.is_file():
                    return str(library.resolve())
    raise NativeUnavailableError("verified PDFium binary is unavailable")


class PdfiumExecutor:
    """Create after Celery forks; one supervised pool per worker child."""

    def __init__(
        self,
        *,
        workers: int | None = None,
        memory_cap_bytes: int = 256 * 1024 * 1024,
        total_memory_bytes: int = 512 * 1024 * 1024,
        engine: Literal["pdfium", "in_core"] = "pdfium",
    ) -> None:
        if engine not in {"pdfium", "in_core"}:
            raise ValueError("invalid PDF engine")
        self._library = _pdfium_library() if engine == "pdfium" else ""
        self._engine = engine
        self._pool = PdfProcessPool(
            workers=workers,
            memory_cap_bytes=memory_cap_bytes,
            total_memory_bytes=total_memory_bytes,
        )
        self.peak_rss_bytes = 0

    def extract_pdf(
        self,
        data: bytes,
        *,
        budget: RuntimeBudget = _DEFAULT_BUDGET,
        cancellation: CancellationHandle | None = None,
    ) -> CanonicalDocument:
        result, peak = self._pool.extract(
            data,
            library=self._library,
            engine=self._engine,
            budget=budget,
            cancellation=cancellation,
        )
        self.peak_rss_bytes = peak
        return CanonicalDocument.from_render_json(result)


@lru_cache(maxsize=1)
def _default_pdf_executor(worker_pid: int) -> tuple[PdfiumExecutor, RuntimeBudget]:
    """Fork-aware lazy default; all documents in one Celery child share its slots."""
    from app.core.config import get_settings

    return configured_pdfium_executor(get_settings())


class NativeExecutor:
    """Create lazily AFTER Celery forks; reuse one executor per worker child."""

    def __init__(self, *, threads: int = 2, max_documents: int = 1) -> None:
        extension = _extension()
        if extension is None:
            raise NativeUnavailableError("native ingestion extension is unavailable")
        self._runtime = extension.Runtime(threads, max_documents)

    def extract_pdf(
        self,
        data: bytes,
        *,
        budget: RuntimeBudget = _DEFAULT_BUDGET,
        cancellation: CancellationHandle | None = None,
    ) -> CanonicalDocument:
        token = cancellation or CancellationHandle()
        return CanonicalDocument.from_render_json(
            self._runtime.extract_pdf(data, json.dumps(asdict(budget)), token._token)
        )

    def run_units(
        self,
        units: tuple[str, ...],
        *,
        budget: RuntimeBudget = _DEFAULT_BUDGET,
        cancellation: CancellationHandle | None = None,
    ) -> ComputedUnits:
        token = cancellation or CancellationHandle()
        return ComputedUnits.from_json(
            self._runtime.run_units(
                json.dumps(units, ensure_ascii=False), json.dumps(asdict(budget)), token._token
            )
        )

    def stream_units(
        self,
        windows: Iterable[tuple[str, ...]],
        *,
        budget: RuntimeBudget = _DEFAULT_BUDGET,
        cancellation: CancellationHandle | None = None,
    ) -> Iterator[ComputedUnits]:
        """Python reads input/persists output; a shared native context bounds the document."""
        token = cancellation or CancellationHandle()
        session = self._runtime.open_document(json.dumps(asdict(budget)), token._token)
        for units in windows:
            yield ComputedUnits.from_json(
                self._runtime.run_window(json.dumps(units, ensure_ascii=False), session)
            )


def configured_native_executor(settings: Settings) -> tuple[NativeExecutor, RuntimeBudget]:
    """Worker wiring seam; call after fork and only on an approved native arm."""
    executor = NativeExecutor(
        threads=settings.native_ingestion_threads,
        max_documents=settings.native_ingestion_max_documents,
    )
    budget = RuntimeBudget(
        max_input_bytes=settings.native_ingestion_max_input_bytes,
        max_memory_bytes=settings.native_ingestion_max_memory_bytes,
        max_output_chars=settings.native_ingestion_max_output_chars,
        max_work_units=settings.native_ingestion_max_work_units,
        timeout_ms=settings.native_ingestion_timeout_ms,
    )
    return executor, budget


def chunk_canonical(
    document: CanonicalDocument,
    *,
    settings: Settings,
    budget: RuntimeBudget = _DEFAULT_BUDGET,
    cancellation: CancellationHandle | None = None,
) -> ChunkedDocument:
    """Explicit candidate computation; production routing remains in #687."""
    from app.ingestion.tokenizer_artifact import load_tokenizer_artifact

    if settings.native_ingestion_tokenizer_model != settings.llm_embedding_model:
        raise ValueError("tokenizer model must match the configured embedding model")
    artifact = load_tokenizer_artifact(
        settings.native_ingestion_tokenizer_path,
        sha256=settings.native_ingestion_tokenizer_sha256,
    )
    extension = _extension()
    if extension is None:
        raise NativeUnavailableError("native ingestion extension is unavailable")
    token = cancellation or CancellationHandle()
    return ChunkedDocument.from_json(
        extension.chunk_document(
            document.document_json,
            artifact,
            json.dumps(
                {
                    "max_tokens": settings.native_ingestion_chunk_tokens,
                    "max_chars": settings.native_ingestion_chunk_chars,
                    "overlap_chars": settings.native_ingestion_overlap_chars,
                    "embedding_model": settings.llm_embedding_model,
                }
            ),
            json.dumps(asdict(budget)),
            token._token,
        )
    )


def normalize_canonical(
    document: CanonicalDocument,
    *,
    budget: RuntimeBudget = _DEFAULT_BUDGET,
    cancellation: CancellationHandle | None = None,
) -> NormalizedDocument:
    """Candidate derived normalization; original evidence/spans are retained."""
    extension = _extension()
    if extension is None:
        raise NativeUnavailableError("native ingestion extension is unavailable")
    token = cancellation or CancellationHandle()
    return NormalizedDocument.from_json(
        extension.normalize_document(
            document.document_json, json.dumps(asdict(budget)), token._token
        )
    )


def configured_pdfium_executor(settings: Settings) -> tuple[PdfiumExecutor, RuntimeBudget]:
    """Inert pool configuration for opt-in PDF orchestration after fork."""
    executor = PdfiumExecutor(
        workers=settings.native_pdf_workers or None,
        memory_cap_bytes=settings.native_pdf_worker_memory_bytes,
        total_memory_bytes=settings.native_pdf_pool_memory_bytes,
    )
    budget = RuntimeBudget(
        max_input_bytes=settings.native_ingestion_max_input_bytes,
        max_memory_bytes=settings.native_ingestion_max_memory_bytes,
        max_output_chars=settings.native_ingestion_max_output_chars,
        max_work_units=settings.native_ingestion_max_work_units,
        timeout_ms=settings.native_ingestion_timeout_ms,
    )
    return executor, budget


@dataclass(frozen=True, slots=True)
class PdfCandidateResult:
    text: str
    route: Literal["python", "native"]
    canonical: CanonicalDocument | None = None
    native_error: str | None = None
    text_equal: bool | None = None


class PdfNeedsOcrError(Exception):
    """A native PDF has incomplete text; orchestration must handle OCR explicitly."""

    code = "needs_ocr"


def parse_pdf_candidate(
    data: bytes,
    *,
    mode: Literal["python", "shadow", "native"] = "python",
    executor: NativeExecutor | PdfiumExecutor | None = None,
    budget: RuntimeBudget | None = None,
    cancellation: CancellationHandle | None = None,
) -> PdfCandidateResult:
    """Independent PDF opt-in seam for #669/#687; existing parsers stay authoritative.

    Shadow returns the exact Python text even when the candidate fails. Native
    failures propagate; a needs-OCR/partial document never becomes empty success.
    Nothing logs content, persists data, or activates an extraction generation.
    """
    if mode not in {"python", "shadow", "native"}:
        raise ValueError("invalid PDF candidate mode")
    from app.ingestion.parsers import parse_document

    if mode == "python":
        return PdfCandidateResult(parse_document(data, mime_type="application/pdf"), "python")
    baseline = parse_document(data, mime_type="application/pdf") if mode == "shadow" else ""
    try:
        if executor is None and not native_available():
            raise NativeUnavailableError("native ingestion extension is unavailable")
        selected: NativeExecutor | PdfiumExecutor
        if executor is None:
            selected, configured_budget = _default_pdf_executor(os.getpid())
        else:
            selected, configured_budget = executor, _DEFAULT_BUDGET
        document = selected.extract_pdf(
            data,
            budget=budget if budget is not None else configured_budget,
            cancellation=cancellation,
        )
        if mode == "native":
            if json.loads(document.generation_json)["outcome"] != "indexed":
                raise PdfNeedsOcrError("PDF pages need OCR")
            return PdfCandidateResult(document.rendered_text, "native", document)
        return PdfCandidateResult(
            baseline, "python", document, text_equal=baseline == document.rendered_text
        )
    except Exception as error:
        if mode == "native":
            raise
        code = (
            "native_unavailable" if isinstance(error, NativeUnavailableError) else "native_failed"
        )
        return PdfCandidateResult(baseline, "python", native_error=code)


async def extract_format_candidate(
    data: bytes, *, mime_type: str, settings: Settings
) -> CandidateExtraction:
    """Only already-landed adapters; no recognition-based parser promotion."""
    import asyncio

    from app.domain.ingestion_shadow import source_format

    if source_format(mime_type) != "pdf":
        raise NativeUnavailableError("native format adapter is not in this dependency stack")
    executor, budget = configured_pdfium_executor(settings)
    document = await asyncio.to_thread(executor.extract_pdf, data, budget=budget)
    outcome = json.loads(document.generation_json).get("outcome", "failed")
    return CandidateExtraction(document.rendered_text, str(outcome), len(document.blocks), document)


def candidate_identity(settings: Settings) -> dict[str, object]:
    """Checkpoint identity covers implementation, pinned library and resource ceilings."""
    root = Path(__file__).resolve().parents[3]
    names = [
        "backend/app/ingestion/native.py",
        "backend/app/ingestion/_pdf_pool.py",
        "backend/app/ingestion/_pdf_worker.py",
        "rust/Cargo.lock",
        "rust/pdfium-binaries.json",
    ]
    source = root / "rust/crates/lumen-docintel/src"
    paths = [root / name for name in names] + sorted(source.rglob("*.rs"))
    digest = hashlib.sha256()
    extension = _extension()
    if extension is not None and extension.__file__:
        package = Path(extension.__file__).parent
        paths += sorted(
            p for p in package.iterdir() if p.suffix in {".pyd", ".so", ".dylib", ".py"}
        )
    for path in paths:
        if not path.is_file():
            continue  # Wheels need no checkout: compiled module bytes are fingerprinted above.
        digest.update(path.name.encode())
        with path.open("rb") as stream:
            digest.update(hashlib.file_digest(stream, "sha256").digest())

    return {
        "build": digest.hexdigest(),
        "limits": [
            settings.native_ingestion_max_input_bytes,
            settings.native_ingestion_max_memory_bytes,
            settings.native_ingestion_max_output_chars,
            settings.native_ingestion_max_work_units,
            settings.native_ingestion_timeout_ms,
            settings.native_pdf_workers,
            settings.native_pdf_worker_memory_bytes,
            settings.native_pdf_pool_memory_bytes,
        ],
    }
