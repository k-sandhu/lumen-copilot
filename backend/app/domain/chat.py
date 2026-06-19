"""Domain types for the chat answer path (CC-6 #24 / CC-11 #26).

Pure, frozen dataclasses — **no ORM, no framework imports** (backend/AGENTS.md:
``domain/`` is pure). These are the shapes the chat runtime (``services/``)
produces and the WS layer relays; they mirror the ``contracts/`` chat payloads
(``ChatCitation``, ``ChatDoneData``, …) without depending on a wire library.

The headline type is :class:`GroundedCitation`: a resolvable, passage-level
reference to a **permitted** retrieved passage (INV-3). A citation is *only* ever
built from a :class:`~app.domain.retrieval.RetrievedPassage` the permission filter
already returned, so by construction it can never point outside the asking user's
allow-set — there is no constructor that takes a raw chunk id from the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from app.domain.retrieval import RetrievedPassage


@dataclass(frozen=True, slots=True)
class GroundedCitation:
    """A passage-level citation the answer asserts (INV-3, CC-11).

    Mirrors the contract ``Citation`` / WS ``ChatCitation`` shapes. ``char_start``
    / ``char_end`` are the **source document** offsets (the chunk's span), so the
    citation deep-links back to the originating document + span (CC-11 AC-3). The
    ``id`` is assigned when the citation row is persisted; pre-persist it is the
    nil UUID. Built only via :meth:`from_passage` from a permitted retrieved
    passage — the permission guarantee is inherited, never re-derived.
    """

    document_id: UUID
    document_name: str
    chunk_id: UUID
    snippet: str
    char_start: int
    char_end: int
    score: float | None = None
    id: UUID | None = None

    @classmethod
    def from_passage(cls, passage: RetrievedPassage) -> GroundedCitation:
        """Build a citation from a permitted retrieved passage (INV-3).

        Carries the passage's source provenance (document id/name) and its source
        span verbatim, so the citation resolves to exactly the text the model was
        shown. Because the input is a :class:`RetrievedPassage`, which only ever
        describes a permitted passage, the citation cannot reference denied
        content.
        """
        return cls(
            document_id=passage.document_id,
            document_name=passage.document_name,
            chunk_id=passage.chunk_id,
            snippet=passage.text,
            char_start=passage.char_start,
            char_end=passage.char_end,
            score=passage.score,
        )


@dataclass(frozen=True, slots=True)
class AnswerResult:
    """The terminal outcome of one grounded answer turn (for ``done``/persistence).

    ``text`` is the full assembled answer; ``citations`` are the permitted
    passages it cited (possibly empty — an honest "I couldn't find it" carries
    zero citations, and that is shown as such, never papered over with a
    fabricated reference). ``finish_reason`` is the model's terminal reason.
    """

    text: str
    finish_reason: str
    citations: tuple[GroundedCitation, ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @property
    def is_grounded(self) -> bool:
        """True iff the answer carries at least one resolvable citation."""
        return len(self.citations) > 0


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """A record of one retrieval tool the agent ran (surfaced as WS events).

    ``call_id`` correlates the ``tool_call`` and ``tool_result`` WS events;
    ``hit_count`` is how many results the tool returned (0 = "found nothing").
    """

    call_id: str
    tool: str
    args: dict[str, object] = field(default_factory=dict)
    hit_count: int = 0
    summary: str | None = None
