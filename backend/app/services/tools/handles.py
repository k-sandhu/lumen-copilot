"""Plain conversation handles: source identity, never authorization."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from uuid import UUID

from markdown_it import MarkdownIt
from markdown_it.rules_inline.state_inline import StateInline

from app.core.errors import ValidationError
from app.domain.retrieval import RetrievedPassage


class EvidenceHandles:
    """An answer's registry loaded only from its own tenant/owner conversation.

    ``first`` is a durably reserved counter range, so aborts leave gaps rather
    than recycle identities. Corpus entries contain IDs and hashes only; every
    read rehydrates through retrieval using the current permissions and scope.
    """

    def __init__(
        self,
        first: int = 1,
        existing: dict[str, dict[str, object]] | None = None,
        capacity: int = 1000,
    ) -> None:
        self.entries = deepcopy(existing or {})
        self.created: dict[str, dict[str, object]] = {}
        self._next = first
        self._capacity = capacity

    def _issue(self, prefix: str, entry: dict[str, object]) -> str:
        for handle, value in self.entries.items():
            if value == entry:
                return handle
        if len(self.created) >= self._capacity:
            raise ValidationError(
                "Evidence handle budget exhausted.", code="handle_budget_exhausted"
            )
        while any(f"{kind}{self._next}" in self.entries for kind in "SDW"):
            self._next += 1
        handle = f"{prefix}{self._next}"
        self._next += 1
        self.entries[handle] = entry
        self.created[handle] = entry
        return handle

    def passage(self, passage: RetrievedPassage) -> str:
        entry: dict[str, object] = {
            "kind": "passage",
            "document_id": str(passage.document_id),
            "chunk_id": str(passage.chunk_id),
            "char_start": passage.char_start,
            "char_end": passage.char_end,
            "fingerprint": hashlib.sha256(passage.text.encode()).hexdigest(),
        }
        provenance = {
            "time_start_ms": passage.time_start_ms,
            "time_end_ms": passage.time_end_ms,
            "transcript_segment_id": (
                str(passage.transcript_segment_id)
                if passage.transcript_segment_id is not None
                else None
            ),
            "speaker_id": passage.speaker_id,
            "speaker_name": passage.speaker_name,
        }
        if any(value is not None for value in provenance.values()):
            entry["provenance_fingerprint"] = hashlib.sha256(
                json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        return self._issue("S", entry)

    def document(self, document_id: UUID) -> str:
        return self._issue("D", {"kind": "document", "document_id": str(document_id)})

    def web(self, url: str, title: str, text: str) -> str:
        return self._issue("W", {"kind": "web", "url": url, "title": title, "text": text})

    def resolve(self, handle: str) -> dict[str, object] | None:
        entry = self.entries.get(handle)
        return deepcopy(entry) if entry is not None else None

    def document_id(self, handle: str) -> UUID | None:
        """Resolve navigation identity from this conversation, never a grant."""
        entry = self.entries.get(handle)
        if (
            not re.fullmatch(r"D[1-9][0-9]*", handle)
            or not entry
            or entry.get("kind") != "document"
        ):
            return None
        try:
            return UUID(str(entry["document_id"]))
        except (KeyError, ValueError):
            return None


_HANDLE = re.compile(r"\[([SDW][1-9][0-9]*)\]")


def _citation_marker(state: StateInline, silent: bool) -> bool:
    """Run after CommonMark's links/images/code rules, recording raw positions."""
    # Link-label lookahead must see ordinary brackets, not a nested token.
    if silent or state.linkLevel:
        return False
    match = _HANDLE.match(state.src, state.pos, state.posMax)
    if match is None:
        return False
    token = state.push("citation_marker", "", 0)
    token.content = match.group(0)
    token.meta["offset"] = state.pos
    state.pos = match.end()
    return True


def select_cited_handles(answer: str, available: set[str]) -> tuple[str, list[str]]:
    """Strip invalid markers and select only visible S/W claims in first-use order."""
    selected: list[str] = []
    removed: list[tuple[int, int]] = []
    parser = MarkdownIt("commonmark")
    parser.inline.ruler.after("image", "citation_marker", _citation_marker)
    line_starts = [0, *(match.end() for match in re.finditer(r"\r\n|\r|\n", answer)), len(answer)]
    for token in parser.parse(answer):
        if token.type != "inline" or token.map is None:
            continue
        # CommonMark strips block prefixes (quotes/lists/headings), but retains
        # raw marker order in each inline block, including code/link data. Map
        # every occurrence before considering eligibility so identical literal
        # and citation markers cannot be confused. Edit only the original spans.
        original = list(
            _HANDLE.finditer(answer, line_starts[token.map[0]], line_starts[token.map[1]])
        )
        offsets = {
            local.start(): source.span()
            for local, source in zip(_HANDLE.finditer(token.content), original, strict=True)
        }
        for child in token.children or ():
            if child.type != "citation_marker":
                continue
            handle = child.content[1:-1]
            if handle.startswith("D") or handle not in available:
                removed.append(offsets[child.meta["offset"]])
            elif handle not in selected:
                selected.append(handle)
    for start, end in reversed(removed):
        answer = answer[:start] + answer[end:]
    return answer, selected
