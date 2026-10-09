"""Shadow computation cannot change the live baseline, including on sink failure."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from app.domain.ingestion_shadow import (
    CandidateExtraction,
    FormatMode,
    RoutedExtraction,
    ShadowComparison,
)
from app.ingestion.native import NativeUnavailableError, PdfWorkerError

log = structlog.get_logger(__name__)


async def extract_with_mode(
    *,
    mode: FormatMode,
    source_format: str,
    baseline: Callable[[], Awaitable[str]],
    candidate: Callable[[], Awaitable[CandidateExtraction]],
    record: Callable[[ShadowComparison], Awaitable[None]],
    max_chars: int = 2_000_000,
) -> RoutedExtraction:
    if mode == "python":
        return RoutedExtraction(await baseline())
    if mode not in {"shadow", "native"}:
        raise ValueError("invalid extraction mode")
    live = ""
    baseline_error: Exception | None = None
    if mode == "shadow":
        try:
            live = await baseline()
        except Exception as error:
            baseline_error = error
    value: CandidateExtraction | None = None
    failure: str | None = None
    try:
        value = await candidate()
        if (
            not isinstance(value, CandidateExtraction)
            or not isinstance(value.text, str)
            or len(value.text) > max_chars
            or type(value.block_count) is not int
            or value.block_count < 0
            or value.outcome
            not in {"indexed", "partial", "needs_ocr", "unsupported", "encrypted", "failed"}
        ):
            raise ValueError("invalid bounded candidate")
        if mode == "native" and value.outcome != "indexed":
            raise ValueError("native extraction must be complete")
    except Exception as error:
        if mode == "native":
            raise
        value = None
        failure = (
            "native_unavailable" if isinstance(error, NativeUnavailableError) else "native_failed"
        )
        if isinstance(error, PdfWorkerError):
            allowed = {
                "budget",
                "timeout",
                "encrypted",
                "parse",
                "invalid_structure",
                "native_panic",
                "output_limit",
                "worker_memory",
                "worker_failed",
            }
            failure = error.code if error.code in allowed else "native_failed"
    if mode == "native":
        assert value is not None
        return RoutedExtraction(value.text, value.canonical)
    comparison = ShadowComparison(
        source_format,
        value.outcome if value else "failed",
        len(live),
        len(value.text) if value else 0,
        baseline_error is None and value is not None and live == value.text,
        (
            sum(a != b for a, b in zip(live, value.text, strict=False))
            + abs(len(live) - len(value.text))
        )
        if value and baseline_error is None
        else (len(live) if baseline_error is None else 0),
        value.block_count if value else 0,
        failure,
        baseline_error is not None,
    )
    try:
        await record(comparison)
    except Exception:
        # A safe aggregate operational event, never exception text or source identity.
        log.warning("ingestion.shadow_diagnostic_unrecorded", source_format=source_format, count=1)
    if baseline_error is not None:
        raise baseline_error
    return RoutedExtraction(live, comparison=comparison)
