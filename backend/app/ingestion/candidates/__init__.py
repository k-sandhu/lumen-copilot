"""Drop-in candidate descriptions; extension imports stay in native.py."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pkgutil import iter_modules
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.config import Settings


@dataclass(frozen=True)
class Candidate:
    family: str
    mimes: frozenset[str]
    formats: frozenset[str]


def candidates() -> tuple[Candidate, ...]:
    return tuple(
        import_module(f"{__name__}.{item.name}").CANDIDATE
        for item in sorted(iter_modules(__path__), key=lambda item: item.name)
        if not item.name.startswith("_")
    )


def upload_types(settings: Settings) -> frozenset[str]:
    from app.ingestion.native import native_available

    registered = candidates()
    gated = frozenset(mime for c in registered for mime in c.mimes) - {
        "text/plain",
        "text/markdown",
    }
    enabled = frozenset(
        mime
        for c in registered
        if getattr(settings, f"native_{c.family}_enabled", False) and native_available()
        for mime in c.mimes
    )
    return (settings.upload_allowed_content_types - gated) | enabled


def configured_parse(data: bytes, mime: str, settings: Settings, baseline: str | None) -> str:
    """Cutover is explicit; a shadow failure cannot change the baseline result."""
    import structlog

    from app.domain.native_runtime import RuntimeBudget
    from app.ingestion import native
    from app.ingestion.parsers import DocumentParseError

    for candidate in candidates():
        if mime not in candidate.mimes:
            continue
        enabled = getattr(settings, f"native_{candidate.family}_enabled", False)
        shadow = getattr(settings, f"native_{candidate.family}_shadow", False)
        if not enabled and not shadow:
            break
        try:
            if not native.native_available():
                raise native.NativeUnavailableError("native ingestion extension is unavailable")
            detected = native.detect_content(data, declared_mime=mime)
            if detected.format not in candidate.formats:
                raise DocumentParseError("content does not match candidate format")
            document = native.extract_candidate(
                data,
                family=candidate.family,
                mime=mime,
                budget=RuntimeBudget(
                    max_input_bytes=settings.native_ingestion_max_input_bytes,
                    max_memory_bytes=settings.native_ingestion_max_memory_bytes,
                    max_output_chars=settings.native_ingestion_max_output_chars,
                    max_work_units=settings.native_ingestion_max_work_units,
                    timeout_ms=settings.native_ingestion_timeout_ms,
                ),
            )
            if enabled:
                return document.rendered_text
            structlog.get_logger().info(
                "native_ingestion_shadow",
                family=candidate.family,
                baseline_available=baseline is not None,
                equal=document.rendered_text == baseline,
                candidate_chars=len(document.rendered_text),
                baseline_chars=len(baseline) if baseline is not None else None,
            )
        except Exception as exc:  # noqa: BLE001 — optional candidate boundary
            if enabled:
                raise DocumentParseError("native candidate extraction failed") from exc
            structlog.get_logger().info(
                "native_ingestion_shadow_failed",
                family=candidate.family,
                error_type=type(exc).__name__,
            )
        break
    if baseline is None:
        raise DocumentParseError("format has no enabled parser")
    return baseline
