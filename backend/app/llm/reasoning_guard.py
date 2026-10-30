"""Bounded provider-output filter shared by streaming and stored replay (#573).

Only provider text enters this filter. User text stays authored data. Tagged
reasoning is suppressed before publication across arbitrary chunk boundaries;
exact internal control sentinels suppress the remainder of the completion.
This deliberately does not classify untagged reasoning prose.
"""

from __future__ import annotations

import re

_TAGS = ("think", "thinking", "reasoning", "reflection")
_OPEN = re.compile(r"<\s*(" + "|".join(_TAGS) + r")\b[^>]*>", re.IGNORECASE)
_CLOSE = re.compile(r"<\s*/\s*(" + "|".join(_TAGS) + r")\s*>", re.IGNORECASE)
_CONTROL = re.compile(
    r"<\|[^|]{0,64}\|>|<\s*/?\s*s\s*>|<\s*/?\s*start_of_turn\s*>" r"|\[/?INST\]|<<\s*/?SYS\s*>>",
    re.IGNORECASE,
)
_SENTINELS = (
    "you maintain a rolling summary",
    "output only the summary",
    "[conversation memory",
    "return only one json object with exactly",
)
_PREFIXES = frozenset(p[:i] for p in _SENTINELS for i in range(1, len(p) + 1))
_MAX_MARKER_CHARS = 256
_MAX_NESTING = 32


def _marker_candidate(text: str) -> bool:
    """Whether text can still be one of the supported markup families."""
    lowered = text.lower()
    if lowered.startswith("["):
        return any(t.startswith(lowered) for t in ("[inst]", "[/inst]"))
    head = lowered[1:].lstrip()
    if head.startswith("|"):
        return True
    if head.startswith("<"):
        head = head[1:].lstrip().removeprefix("/")
        return "sys".startswith(head) or (
            head.startswith("sys") and re.fullmatch(r"\s*>{0,2}", head[3:]) is not None
        )
    closing = head.startswith("/")
    head = head.removeprefix("/").lstrip()
    for tag in (*_TAGS, "s", "start_of_turn"):
        if tag.startswith(head):
            return True
        if head.startswith(tag):
            tail = head[len(tag) :]
            if tag in _TAGS and not closing:
                # Opening reasoning tags allow attributes after a word boundary.
                if re.match(r"\W", tail):
                    return True
            elif re.fullmatch(r"\s*>?", tail) is not None:
                return True
    return False


def _suspicious_partial(text: str) -> bool:
    if text.startswith("<") and not text[1:].lstrip().lstrip("/").lstrip():
        return False
    return _marker_candidate(text)


class ReasoningGuard:
    """Feed chunks, publish the return, and flush on normal EOF.

    No hidden body is accumulated. Marker candidates are bounded to 256
    characters, nesting to 32, sentinel lookbehind to the longest sentinel.
    Excessive/malformed hidden markup fails closed for the rest of the turn.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._stack: list[str] = []
        self._sentinel_pending = ""
        self._discard = False
        self.blocked_blocks = 0

    @property
    def blocked(self) -> bool:
        return self.blocked_blocks > 0

    @property
    def continuation_safe(self) -> bool:
        """A fresh guard may continue only when no hidden state is outstanding."""
        return not (self._pending or self._stack or self._sentinel_pending or self._discard)

    def _visible(self, text: str) -> str:
        out: list[str] = []
        for char in text:
            if self._discard:
                break
            self._sentinel_pending += char
            while self._sentinel_pending:
                lower = self._sentinel_pending.lower()
                if lower in _SENTINELS:
                    self._discard = True
                    self._sentinel_pending = ""
                    self.blocked_blocks += 1
                    break
                if lower in _PREFIXES:
                    break
                out.append(self._sentinel_pending[0])
                self._sentinel_pending = self._sentinel_pending[1:]
        return "".join(out)

    def feed(self, chunk: str) -> str:
        out: list[str] = []
        for char in chunk:
            if self._discard:
                break
            if not self._pending:
                if char in "<[":
                    self._pending = char
                elif not self._stack:
                    out.append(self._visible(char))
                continue
            # Keep <<SYS>> together while recognizing a following <think>.
            if char == "<" and self._pending != "<":
                if not self._stack:
                    out.append(self._visible(self._pending))
                self._pending = "<"
                continue
            self._pending += char
            pending = self._pending
            if len(pending) > _MAX_MARKER_CHARS:
                self._discard = True
                self.blocked_blocks += 1
                self._pending = ""
                continue
            # Release ordinary comparisons/brackets as soon as markup is impossible.
            # Only actual marker candidates may reach the bounded fail-closed buffer.
            if not _marker_candidate(pending):
                if not self._stack:
                    out.append(self._visible(pending))
                self._pending = ""
                continue
            if char not in ">]":
                continue
            if pending.startswith("<<") and not pending.endswith(">>"):
                continue
            self._pending = ""
            opening, closing = _OPEN.fullmatch(pending), _CLOSE.fullmatch(pending)
            if opening:
                self.blocked_blocks += 1
                if len(self._stack) >= _MAX_NESTING:
                    self._discard = True
                else:
                    self._stack.append(opening[1].lower())
            elif closing:
                self.blocked_blocks += 1
                if self._stack and self._stack[-1] == closing[1].lower():
                    self._stack.pop()
            elif _CONTROL.fullmatch(pending):
                self.blocked_blocks += 1
            elif not self._stack:
                out.append(self._visible(pending))
        return "".join(out)

    def flush(self) -> str:
        pending, self._pending = self._pending, ""
        if self._discard or self._stack:
            self._stack.clear()
            self._sentinel_pending = ""
            return ""
        if pending and _suspicious_partial(pending):
            self.blocked_blocks += 1
            pending = ""
        out = self._visible(pending)
        if not self._discard:
            out += self._sentinel_pending
        self._sentinel_pending = ""
        return out


def sanitize_provider_text(text: str) -> str:
    """Complete-text replay uses exactly the streaming state machine."""
    guard = ReasoningGuard()
    return guard.feed(text) + guard.flush()


def strip_reasoning(text: str) -> str:
    return sanitize_provider_text(text)
