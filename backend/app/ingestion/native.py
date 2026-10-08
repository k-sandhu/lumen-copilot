"""Optional native ingestion facade (ADR-0026); the sole extension importer."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType


def _extension() -> ModuleType | None:
    try:
        return import_module("lumen_docintel")
    except ImportError:
        return None


def native_available() -> bool:
    """Availability is distinct from a format's approved cutover status."""
    return _extension() is not None
