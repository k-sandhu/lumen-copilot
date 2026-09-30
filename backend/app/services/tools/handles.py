"""Plain conversation handles: source identity, never authorization."""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from uuid import UUID

from markdown_it import MarkdownIt

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
        return self._issue(
            "S",
            {
                "kind": "passage",
                "document_id": str(passage.document_id),
                "chunk_id": str(passage.chunk_id),
                "char_start": passage.char_start,
                "char_end": passage.char_end,
                "fingerprint": hashlib.sha256(passage.text.encode()).hexdigest(),
            },
        )

    def document(self, document_id: UUID) -> str:
        return self._issue("D", {"kind": "document", "document_id": str(document_id)})

    def web(self, url: str, title: str, text: str) -> str:
        return self._issue("W", {"kind": "web", "url": url, "title": title, "text": text})

    def resolve(self, handle: str) -> dict[str, object] | None:
        entry = self.entries.get(handle)
        return deepcopy(entry) if entry is not None else None


_HANDLE = re.compile(r"\[([SDW][1-9][0-9]*)\]")


def select_cited_handles(answer: str, available: set[str]) -> tuple[str, list[str]]:
    """Strip invalid markers and select only visible S/W claims in first-use order."""
    selected: list[str] = []
    # CommonMark text nodes agree with the frontend's remark text-node walk:
    # code, HTML and link destinations/labels are data, never source claims.
    for token in MarkdownIt("commonmark").parse(answer):
        link_depth = 0
        for child in token.children or ():
            if child.type == "link_open":
                link_depth += 1
            elif child.type == "link_close":
                link_depth -= 1
            elif child.type == "text" and not link_depth:
                for match in _HANDLE.finditer(child.content):
                    handle = match.group(1)
                    if (
                        not handle.startswith("D")
                        and handle in available
                        and handle not in selected
                    ):
                        selected.append(handle)

    def resolve(match: re.Match[str]) -> str:
        handle = match.group(1)
        if handle.startswith("D") or handle not in available:
            return ""
        return match.group(0)

    return _HANDLE.sub(resolve, answer), selected
