"""Portable document navigation results; no storage or provider types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.retrieval import RetrievedPassage


@dataclass(frozen=True, slots=True)
class DiscoveryDocument:
    document_id: UUID
    title: str
    filename: str
    source_path: str | None
    source: str
    mime_type: str
    created_at: datetime
    metadata: dict[str, object]
    source_modified_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DocumentPage:
    items: tuple[DiscoveryDocument, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class DocumentRead:
    document_id: UUID
    document_name: str
    passages: tuple[RetrievedPassage, ...]
    total_length: int
    returned_start: int
    returned_end: int
    next_start: int | None
