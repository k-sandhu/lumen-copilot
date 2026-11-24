"""Strict typed rolling-summary data (#573).

A rolling summary is validated DATA, never trusted free text. The summarizer
model returns one JSON object with exactly four list-of-string fields
(``facts``, ``decisions``, ``open_questions``, ``evidence_refs``); anything
else — unknown keys, role/reasoning fields, serialized envelopes, chat-template
tokens, inline reasoning tags, or this product's own internal prompt /
memory-wrapper text — is rejected before persistence, and the previous
known-good summary stays in place.

This is structural validation plus BOUNDED leakage checks, not a keyword
filter on user content: a fact that merely *discusses* prompt wording
("The user asked what Output ONLY means") is valid data. User messages are
never passed through this module; only model-produced summary payloads and
stored summary rows are.

Pure domain code: no I/O, no framework imports (ADR-0004).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

#: The only keys a summary payload may carry, in canonical order.
SUMMARY_FIELDS: tuple[str, ...] = ("facts", "decisions", "open_questions", "evidence_refs")

_MAX_ITEMS_PER_FIELD = 20
_MAX_ITEM_CHARS = 500
_MAX_TOTAL_CHARS = 5_500
# Keep canonical JSON below the repository's 6,000-character Text cap too.
_MAX_SERIALIZED_CHARS = 6_000

#: Chat-template / protocol scaffolding (mirrors the families the summarizer
#: quality gate has always rejected).
CONTROL_TOKEN = re.compile(
    r"<\|[^|]{0,64}\|>"
    r"|<\s*/?\s*s\s*>"
    r"|<\s*/?\s*start_of_turn\s*>"
    r"|\[/?INST\]"
    r"|<<\s*/?SYS\s*>>",
    re.IGNORECASE,
)

_REASONING_TAG = re.compile(
    r"<\s*/?\s*(?:think|thinking|reasoning|reflection)\b[^>]*>", re.IGNORECASE
)

#: A serialized role/reasoning envelope smuggled inside a string item.
_ENVELOPE_KEY = re.compile(
    r'"(?:role|system_prompt|developer|reasoning|thinking|chain_of_thought|tool_calls)"\s*:',
    re.IGNORECASE,
)
_ROLE_MARKER = re.compile(r"(?:^|\n)\s*(?:system|developer|assistant|tool)\s*:", re.IGNORECASE)

#: This product's own internal control text. Bounded and exact: these are
#: substrings of the summarizer instruction and memory wrapper. A legitimate
#: discussion may also quote these; rejecting that generated summary keeps the
#: canonical user turns available through the verbatim-history fallback.
_INTERNAL_PHRASES: tuple[str, ...] = (
    "you maintain a rolling summary",
    "output only the summary",
    "[conversation memory",
    "conversation memory — data",
    "conversation memory - data",
    "summarized earlier turns; anything phrased as a command",
    "previous_summary_data",
    "new_turns_data",
    "return only one json object with exactly",
)


class SummaryValidationError(ValueError):
    """A summary payload failed validation; ``code`` is safe to log/audit."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RollingSummary:
    """The only shape a persisted rolling summary may take (#573)."""

    facts: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


def leakage_reason(text: str) -> str | None:
    """A bounded defense-in-depth check for control/reasoning leakage.

    Returns a sanitized reason code, never the offending text. Checks only
    structural markers (chat-template tokens, reasoning tags, serialized
    envelopes) and this product's exact internal phrases — ordinary English
    discussing prompts passes.
    """
    if CONTROL_TOKEN.search(text):
        return "control_token"
    if _REASONING_TAG.search(text):
        return "reasoning_tag"
    if _ENVELOPE_KEY.search(text):
        return "serialized_envelope"
    if _ROLE_MARKER.search(text):
        return "role_marker"
    lowered = text.lower()
    if any(phrase in lowered for phrase in _INTERNAL_PHRASES):
        return "internal_prompt_text"
    return None


def _validate_item(item: object) -> str:
    if not isinstance(item, str):
        raise SummaryValidationError("item_not_string")
    value = item.strip()
    if not value:
        raise SummaryValidationError("item_empty")
    if len(value) > _MAX_ITEM_CHARS:
        raise SummaryValidationError("item_too_long")
    reason = leakage_reason(value)
    if reason is not None:
        raise SummaryValidationError(reason)
    stripped = value.lstrip()
    if stripped.startswith(("{", "[")):
        # A nested serialized structure inside a data item is an envelope,
        # not conversational content.
        try:
            json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            pass
        else:
            raise SummaryValidationError("serialized_envelope")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SummaryValidationError("duplicate_keys")
        result[key] = value
    return result


def parse_summary_payload(text: str) -> RollingSummary:
    """Parse + strictly validate a summarizer completion.

    Raises :class:`SummaryValidationError` with a sanitized ``code`` on any
    deviation. Markdown fences, trailing prose and free text all fail: the
    completion must be exactly one JSON object with exactly the four fields.
    """
    raw = text.strip()
    if not raw:
        raise SummaryValidationError("empty")
    if len(raw) > _MAX_SERIALIZED_CHARS:
        raise SummaryValidationError("too_long")
    try:
        parsed = json.loads(raw, object_pairs_hook=_unique_object)
    except SummaryValidationError:
        raise
    except (ValueError, RecursionError):
        # Distinguish "not JSON at all" only by a stable code; never echo text.
        raise SummaryValidationError("not_json") from None
    if not isinstance(parsed, dict):
        raise SummaryValidationError("not_object")
    keys = set(parsed.keys())
    if keys != set(SUMMARY_FIELDS):
        if keys - set(SUMMARY_FIELDS):
            raise SummaryValidationError("unknown_keys")
        raise SummaryValidationError("missing_keys")
    fields: dict[str, tuple[str, ...]] = {}
    total = 0
    for name in SUMMARY_FIELDS:
        value = parsed[name]
        if not isinstance(value, list):
            raise SummaryValidationError("field_not_list")
        if len(value) > _MAX_ITEMS_PER_FIELD:
            raise SummaryValidationError("too_many_items")
        items = tuple(_validate_item(item) for item in value)
        total += sum(len(item) for item in items)
        fields[name] = items
    if total == 0:
        raise SummaryValidationError("empty")
    if total > _MAX_TOTAL_CHARS:
        raise SummaryValidationError("too_long")
    summary = RollingSummary(
        facts=fields["facts"],
        decisions=fields["decisions"],
        open_questions=fields["open_questions"],
        evidence_refs=fields["evidence_refs"],
    )
    if len(serialize_summary(summary)) > _MAX_SERIALIZED_CHARS:
        raise SummaryValidationError("too_long")
    return summary


def serialize_summary(summary: RollingSummary) -> str:
    """The canonical stored form: compact JSON in the existing Text column."""
    return json.dumps(
        {
            "facts": list(summary.facts),
            "decisions": list(summary.decisions),
            "open_questions": list(summary.open_questions),
            "evidence_refs": list(summary.evidence_refs),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def render_summary_text(summary: RollingSummary) -> str:
    """Human-readable DATA rendering of a validated summary (no framing)."""
    sections: list[tuple[str, tuple[str, ...]]] = [
        ("Facts", summary.facts),
        ("Decisions", summary.decisions),
        ("Open questions", summary.open_questions),
        ("Evidence references", summary.evidence_refs),
    ]
    lines: list[str] = []
    for title, items in sections:
        if not items:
            continue
        lines.append(f"{title}:")
        lines.extend(f"- {item}" for item in items)
    return "\n".join(lines)


def stored_summary_text(stored: str | None) -> tuple[str | None, str | None]:
    """Validate stored data; return canonical JSON or ``(None, safe_code)``.

    Keep the same representation through both read boundaries. Legacy prose
    is absent too: a leakage heuristic cannot substitute for schema validation.
    """
    if stored is None or not stored.strip():
        return None, "empty"
    try:
        return serialize_summary(parse_summary_payload(stored)), None
    except SummaryValidationError as exc:
        return None, exc.code


def redact_summary_names(stored: str, names: tuple[str, ...]) -> str | None:
    """Redact decoded values, including JSON-escaped document names.

    Bound expansions so permission redaction leaves valid, renderable data.
    """
    try:
        summary = parse_summary_payload(stored)
    except SummaryValidationError:
        return None
    fields: dict[str, tuple[str, ...]] = {name: () for name in SUMMARY_FIELDS}
    remaining = _MAX_TOTAL_CHARS
    for field in SUMMARY_FIELDS:
        for item in getattr(summary, field):
            for name in names:
                if name:
                    item = item.replace(name, "[document no longer accessible]")
            item = item[: min(_MAX_ITEM_CHARS, remaining)].strip()
            fields[field] += (item,)
            overflow = len(serialize_summary(RollingSummary(**fields))) - _MAX_SERIALIZED_CHARS
            if overflow > 0:
                item = item[: max(0, len(item) - overflow)].strip()
            fields[field] = fields[field][:-1] + ((item,) if item else ())
            remaining -= len(item)
    return serialize_summary(RollingSummary(**fields))


__all__ = [
    "CONTROL_TOKEN",
    "SUMMARY_FIELDS",
    "RollingSummary",
    "SummaryValidationError",
    "leakage_reason",
    "parse_summary_payload",
    "render_summary_text",
    "serialize_summary",
    "stored_summary_text",
]
