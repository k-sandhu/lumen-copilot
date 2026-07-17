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


# --- ask_user — the clarifying question (spec 0006 #429) --------------------

# Bounds for a model-authored question (spec 0006 §3.3). Enforced at parse time
# so an out-of-bounds call becomes a typed tool error the model can recover
# from — never a broken event or an oversized persisted payload.
ASK_USER_MIN_OPTIONS = 2
ASK_USER_MAX_OPTIONS = 4
ASK_USER_MAX_QUESTION_CHARS = 500
ASK_USER_MAX_LABEL_CHARS = 120
ASK_USER_MAX_DESCRIPTION_CHARS = 240


class AskUserValidationError(ValueError):
    """The model's ``ask_user`` arguments don't form a renderable question.

    Raised by :meth:`AskUserQuestion.parse`; the chat runtime turns it into an
    ``ok=False`` tool *result* (the model reads the reason and recovers — the
    turn continues toward a normal answer, spec 0006 §2 / #429 AC-N1).
    """


@dataclass(frozen=True, slots=True)
class AskUserOption:
    """One clickable choice of a clarifying question (spec 0006 #429).

    ``label`` is sent verbatim as the user's reply when clicked, so it must read
    as an answer on its own; ``description`` is optional elaboration for the UI.
    """

    label: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class AskUserQuestion:
    """A clarifying question the model asked instead of answering (spec 0006).

    Mirrors the contract shapes (WS ``ChatAskUser`` minus ``messageId``; REST
    ``AskUserQuestion``). Built only via :meth:`parse`, which enforces the spec
    bounds — so a persisted/emitted question is renderable by construction.
    """

    question: str
    options: tuple[AskUserOption, ...]
    allow_free_text: bool = True

    @classmethod
    def parse(cls, arguments: dict[str, object]) -> AskUserQuestion:
        """Validate raw model tool arguments into a question, or raise.

        Rules (spec 0006 §3.3): non-empty question ≤ 500 chars; 2–4 options
        after trimming and case-insensitive label dedupe; labels ≤ 120 chars;
        descriptions ≤ 240 chars (longer ones are truncated, not fatal — only
        *structural* problems reject). Raises :class:`AskUserValidationError`
        with a reason the model can act on.
        """
        question = str(arguments.get("question") or "").strip()
        if not question:
            raise AskUserValidationError("ask_user requires a non-empty 'question'.")
        if len(question) > ASK_USER_MAX_QUESTION_CHARS:
            raise AskUserValidationError(
                f"ask_user 'question' exceeds {ASK_USER_MAX_QUESTION_CHARS} characters."
            )

        raw_options = arguments.get("options")
        if not isinstance(raw_options, list):
            raise AskUserValidationError("ask_user requires an 'options' array.")
        options: list[AskUserOption] = []
        seen_labels: set[str] = set()
        for raw in raw_options:
            if isinstance(raw, dict):
                label = str(raw.get("label") or "").strip()
                description = str(raw.get("description") or "").strip() or None
            elif isinstance(raw, str):
                # Tolerate the natural model shorthand ["A", "B"] — the intent
                # is unambiguous, so accepting it beats a retry round-trip.
                label, description = raw.strip(), None
            else:
                continue
            # Truncate BEFORE deduplication (#434 review, finding 6): two
            # distinct over-long labels that share a 120-char head would
            # otherwise collapse into identical rendered choices — dedupe must
            # see exactly what the user will see.
            label = label[:ASK_USER_MAX_LABEL_CHARS].strip()
            if not label or label.casefold() in seen_labels:
                continue
            seen_labels.add(label.casefold())
            if description is not None:
                description = description[:ASK_USER_MAX_DESCRIPTION_CHARS]
            options.append(AskUserOption(label=label, description=description))
        if not ASK_USER_MIN_OPTIONS <= len(options) <= ASK_USER_MAX_OPTIONS:
            raise AskUserValidationError(
                "ask_user requires between "
                f"{ASK_USER_MIN_OPTIONS} and {ASK_USER_MAX_OPTIONS} distinct, "
                f"non-empty options (got {len(options)})."
            )

        # A non-boolean allow_free_text is malformed input, contained under
        # INV-8 (#434 review, finding 6): the string "false" must never
        # silently become True. Absent ⇒ the default (free text allowed).
        allow_free_text_raw = arguments.get("allow_free_text", True)
        if not isinstance(allow_free_text_raw, bool):
            raise AskUserValidationError(
                "ask_user 'allow_free_text' must be a boolean when provided."
            )

        return cls(
            question=question,
            options=tuple(options),
            allow_free_text=allow_free_text_raw,
        )

    def to_payload(self) -> dict[str, object]:
        """The portable JSON payload (the ``messages.question`` column shape).

        Snake_case, matching the REST ``AskUserQuestion`` schema — the repository
        stores and returns it verbatim, so the stored shape IS the wire shape.
        """
        return {
            "question": self.question,
            "options": [
                {
                    "label": o.label,
                    **({"description": o.description} if o.description else {}),
                }
                for o in self.options
            ],
            "allow_free_text": self.allow_free_text,
        }

    @classmethod
    def from_payload(cls, payload: object) -> AskUserQuestion | None:
        """Rehydrate from a stored ``messages.question`` payload; ``None`` if unusable.

        Reads are lenient (a malformed row must never 500 a history load — the
        message still renders as plain content); writes are strict via
        :meth:`parse`.
        """
        if not isinstance(payload, dict):
            return None
        question = str(payload.get("question") or "").strip()
        raw_options = payload.get("options")
        if not question or not isinstance(raw_options, list):
            return None
        options = tuple(
            AskUserOption(
                label=str(raw.get("label")),
                description=(str(raw["description"]) if raw.get("description") else None),
            )
            for raw in raw_options
            if isinstance(raw, dict) and str(raw.get("label") or "").strip()
        )
        if not options:
            return None
        return cls(
            question=question,
            options=options,
            allow_free_text=bool(payload.get("allow_free_text", True)),
        )


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
