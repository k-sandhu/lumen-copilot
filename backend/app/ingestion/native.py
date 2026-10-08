"""Optional native ingestion facade (ADR-0026); the sole extension importer."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

from app.domain.canonical import CanonicalDocument


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
