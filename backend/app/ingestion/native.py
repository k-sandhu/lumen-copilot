"""Optional native ingestion facade (ADR-0027); the sole extension importer."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.config import Settings

from app.domain.canonical import CanonicalDocument
from app.domain.document_detection import DetectedDocument
from app.domain.native_chunking import ChunkedDocument
from app.domain.native_normalization import NormalizedDocument
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


class NativeFormatDisabledError(ValueError):
    """A new candidate format has not been accepted in configuration."""


def open_document_route(format_name: str, *, settings: Settings) -> str:
    """Candidate switches cannot promote an unevaluated format to live parsing."""
    if format_name == "odt":
        accepted, shadow, cutover = (
            settings.native_odt_accept,
            settings.native_odt_shadow,
            settings.native_odt_cutover,
        )
    elif format_name == "rtf":
        accepted, shadow, cutover = (
            settings.native_rtf_accept,
            settings.native_rtf_shadow,
            settings.native_rtf_cutover,
        )
    else:
        return "unsupported"
    if not accepted:
        return "unsupported"
    if cutover:
        return "promotion_pending"
    return "native_shadow" if shadow else "candidate_only"


def extract_open_document(
    data: bytes,
    *,
    format_name: str,
    settings: Settings,
    budget: RuntimeBudget = _DEFAULT_BUDGET,
) -> CanonicalDocument:
    """Explicit evaluation entry; immutable domain output, no activation."""
    if open_document_route(format_name, settings=settings) == "unsupported":
        raise NativeFormatDisabledError("native candidate format is disabled")
    extension = _extension()
    if extension is None or not callable(getattr(extension, "extract_open_document", None)):
        raise NativeUnavailableError("native candidate extension is unavailable")
    return CanonicalDocument.from_render_json(
        extension.extract_open_document(data, format_name, json.dumps(asdict(budget)))
    )


def shadow_open_document(
    data: bytes,
    *,
    format_name: str,
    python_text: str,
    settings: Settings,
    budget: RuntimeBudget = _DEFAULT_BUDGET,
) -> tuple[str, dict[str, object]]:
    """Return Python evidence unchanged and bounded content-free comparison."""
    if open_document_route(format_name, settings=settings) != "native_shadow":
        return python_text, {"format": format_name, "candidate_outcome": "disabled"}
    try:
        candidate = extract_open_document(
            data, format_name=format_name, settings=settings, budget=budget
        )
    except Exception:  # noqa: BLE001 — shadow failures cannot alter live evidence
        return python_text, {"format": format_name, "candidate_outcome": "error"}
    return python_text, {
        "format": format_name,
        "candidate_outcome": "computed",
        "text_equal": candidate.rendered_text == python_text,
        "candidate_chars": len(candidate.rendered_text),
    }


class NativeExecutor:
    """Create lazily AFTER Celery forks; reuse one executor per worker child."""

    def __init__(self, *, threads: int = 2, max_documents: int = 1) -> None:
        extension = _extension()
        if extension is None:
            raise NativeUnavailableError("native ingestion extension is unavailable")
        self._runtime = extension.Runtime(threads, max_documents)

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
