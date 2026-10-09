"""Content-free comparison records; positional mismatches are not edit distance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from app.domain.canonical import CanonicalDocument

FormatMode = Literal["python", "shadow", "native"]
FORMATS = frozenset({"pdf", "docx", "pptx", "xlsx", "text", "markdown"})
MIME_FORMATS = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "text/plain": "text",
    "text/markdown": "markdown",
}


def source_format(mime_type: str) -> str:
    return MIME_FORMATS.get(mime_type.split(";", 1)[0].strip().lower(), "unsupported")


@dataclass(frozen=True, slots=True)
class CandidateExtraction:
    text: str
    outcome: str
    block_count: int
    canonical: CanonicalDocument | None = None


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    source_format: str
    status: str
    baseline_chars: int
    candidate_chars: int
    exact_equal: bool
    positional_mismatches: int
    block_count: int
    failure_code: str | None = None
    baseline_failed: bool = False

    @classmethod
    def from_payload(cls, raw: dict[str, object]) -> ShadowComparison:
        raw = dict(raw)
        raw.setdefault("baseline_failed", False)
        expected = {
            "baseline_failed",
            "source_format",
            "status",
            "baseline_chars",
            "candidate_chars",
            "exact_equal",
            "positional_mismatches",
            "block_count",
            "failure_code",
        }
        if (
            set(raw) != expected
            or raw["source_format"] not in FORMATS
            or raw["status"]
            not in {"indexed", "partial", "needs_ocr", "unsupported", "encrypted", "failed"}
        ):
            raise ValueError("invalid shadow schema")
        for name in ("baseline_chars", "candidate_chars", "positional_mismatches", "block_count"):
            if type(raw[name]) is not int or cast(int, raw[name]) < 0:
                raise ValueError("invalid shadow counters")
        if type(raw["baseline_failed"]) is not bool:
            raise ValueError("invalid baseline diagnostic")
        if type(raw["exact_equal"]) is not bool or raw["failure_code"] not in {
            None,
            "native_unavailable",
            "native_failed",
            "budget",
            "timeout",
            "encrypted",
            "parse",
            "invalid_structure",
            "native_panic",
            "output_limit",
            "worker_memory",
            "worker_failed",
        }:
            raise ValueError("invalid shadow diagnostics")
        return cls(
            cast(str, raw["source_format"]),
            cast(str, raw["status"]),
            cast(int, raw["baseline_chars"]),
            cast(int, raw["candidate_chars"]),
            raw["exact_equal"],
            cast(int, raw["positional_mismatches"]),
            cast(int, raw["block_count"]),
            cast(str | None, raw["failure_code"]),
            raw["baseline_failed"],
        )


@dataclass(frozen=True, slots=True)
class RoutedExtraction:
    text: str
    canonical: CanonicalDocument | None = None
    comparison: ShadowComparison | None = None


@dataclass(frozen=True, slots=True)
class ShadowRecord:
    id: UUID
    document_id: UUID
    comparison: ShadowComparison


class NativeEvidenceLocked(Exception):
    """Published evidence cannot be replaced without immutable generation policy."""
