"""Domain types for retrieval results (CC-1 #45, mission filters #1/#2).

The types the ``retrieval/`` adapter returns to its callers (the chat runtime
#24, the agent tools). Pure, frozen dataclasses — **no ORM, no SQLAlchemy, no
framework imports** (backend/AGENTS.md: ``domain/`` is pure). Like the ``llm/``
domain types, these are what crosses the adapter boundary so no pgvector / SQL
row ever leaks upward (ADR-0004 adapter rule 1).

Every type carries the **source provenance** a citation needs (``document_id`` +
``document_name``) and the **char offsets** into the source document
(``char_start``/``char_end``) — the same offsets the #21 chunker recorded, so a
retrieved passage round-trips to ``source[char_start:char_end]`` and the chat
runtime can cite it (CC-11). They are returned only for passages/documents the
asking principal was permitted to see — the permission filter lives inside
``retrieval/`` and there is no path that returns an unfiltered result (INV-2).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RetrievedPassage:
    """One ranked passage from a hybrid search, with its citation provenance.

    The unit ``search()`` and the ``search_text`` tool return. Carries the chunk
    it came from (``chunk_id``), its source document (``document_id`` +
    ``document_name``) and the exact span in that document
    (``char_start``/``char_end``) so the chat runtime can attach a resolvable
    citation (CC-11 / INV-3). ``score`` is the fused rank score (higher = more
    relevant); ``ord`` is the chunk's position in document order.

    Invariant (INV-2): an instance only ever describes a passage the asking
    principal was permitted to retrieve — the permission filter ran before this
    was constructed.
    """

    chunk_id: UUID
    document_id: UUID
    document_name: str
    ord: int
    text: str
    char_start: int
    char_end: int
    score: float
    # Media provenance (spec 0008 / #571). The pair is null for ordinary
    # documents; citation policy validates paired, ordered, in-duration values
    # before an answer may expose them.
    time_start_ms: int | None = None
    time_end_ms: int | None = None
    transcript_segment_id: UUID | None = None
    speaker_id: str | None = None
    speaker_name: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentMatch:
    """A document matched by filename / metadata (the ``search_documents`` tool).

    A document-level hit (not a passage): the id + name the chat runtime needs to
    reference or then fetch with ``get_document``. ``score`` is a lexical match
    score (higher = better). Only documents the principal owns/may see are ever
    returned (INV-2).
    """

    document_id: UUID
    document_name: str
    score: float


@dataclass(frozen=True, slots=True)
class DocumentText:
    """The full text of a permitted document (the ``get_document`` tool).

    The reassembled plain text of a document the principal is permitted to see,
    with its name for citation. ``text`` is the document's chunks concatenated in
    order; ``char_start`` of the first chunk is the document origin so offsets in
    ``text`` line up with the per-chunk spans the chat runtime cites against.
    Returned only when the principal may see the document (INV-2); otherwise the
    tool yields ``None`` (→ the runtime treats it as "not found").
    """

    document_id: UUID
    document_name: str
    text: str
