"""Content-free comparison records; positional mismatches are not edit distance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
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
