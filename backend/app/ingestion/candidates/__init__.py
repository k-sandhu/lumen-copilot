"""Drop-in candidate descriptions; extension imports stay in native.py."""

from __future__ import annotations

from collections.abc import Callable
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
    from app.ingestion.native import candidate_available, native_available

    registered = candidates()
    gated = frozenset(mime for c in registered for mime in c.mimes) - {
        "text/plain",
        "text/markdown",
    }
    enabled = frozenset(
        mime
        for c in registered
        if getattr(settings, f"native_{c.family}_enabled", False)
        and native_available()
        and candidate_available(c.family)
        for mime in c.mimes
    )
    return (settings.upload_allowed_content_types - gated) | enabled


def configured_parse(
    data: bytes, mime: str, settings: Settings, baseline: Callable[[], str] | None
) -> str:
    """Cutover is explicit; a shadow failure cannot change the baseline result."""
    import json

    import structlog

    from app.domain.native_runtime import RuntimeBudget
    from app.ingestion import native
    from app.ingestion.parsers import DocumentParseError

    cached: str | None = None

    def baseline_text() -> str | None:
        nonlocal cached
        if cached is None and baseline is not None:
            cached = baseline()
        return cached

    attempted_cutover = False
    for candidate in candidates():
        if mime not in candidate.mimes:
            continue
        enabled = getattr(settings, f"native_{candidate.family}_enabled", False)
        shadow = getattr(settings, f"native_{candidate.family}_shadow", False)
        attempted_cutover = attempted_cutover or enabled
        if not enabled and not shadow:
            continue
        try:
            if len(data) > settings.native_ingestion_max_input_bytes:
                raise DocumentParseError("native input budget exceeded")
            if not native.native_available():
                raise native.NativeUnavailableError("native ingestion extension is unavailable")
            detected = native.detect_content(data, declared_mime=mime)
            if detected.format not in candidate.formats:
                continue
            if not native.candidate_available(candidate.family):
                raise native.NativeUnavailableError("native candidate is unavailable")
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
                if json.loads(document.generation_json).get("outcome") not in {"success", "empty"}:
                    raise DocumentParseError("native candidate extraction is incomplete")
                return document.rendered_text
            live = baseline_text()
            structlog.get_logger().info(
                "native_ingestion_shadow",
                family=candidate.family,
                baseline_available=live is not None,
                equal=document.rendered_text == live,
                candidate_chars=len(document.rendered_text),
                baseline_chars=len(live) if live is not None else None,
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
    if attempted_cutover:
        raise DocumentParseError("content has no matching enabled native parser")
    live = baseline_text()
    if live is None:
        raise DocumentParseError("format has no enabled parser")
    return live
