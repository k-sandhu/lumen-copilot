"""Optional native ingestion facade (ADR-0027); the sole extension importer."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from app.core.config import Settings

from app.domain.canonical import CanonicalDocument
from app.domain.document_detection import DetectedDocument
from app.domain.native_runtime import ComputedUnits, RuntimeBudget


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


_DEFAULT_BUDGET = RuntimeBudget()


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
    executor: NativeExecutor | None = None,
    budget: RuntimeBudget = _DEFAULT_BUDGET,
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
        document = (executor or NativeExecutor()).extract_pdf(data, budget=budget)
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
