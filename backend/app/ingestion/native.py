"""Optional native ingestion facade (ADR-0027); the sole extension importer."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

from app.domain.canonical import CanonicalDocument
from app.domain.document_detection import DetectedDocument


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
