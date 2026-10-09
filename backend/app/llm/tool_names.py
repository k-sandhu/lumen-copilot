"""Portable provider names; product tool identities never leave this boundary."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from app.core.errors import ValidationError

_SAFE_NAME = re.compile(r"[A-Za-z0-9_]{1,64}\Z", re.ASCII)


class ToolNameMap:
    """One completion's deterministic, collision-checked bidirectional mapping.

    Historical names join offered names so calls/results keep protocol identity
    when a tool is no longer offered. Unknown incoming names stay unknown and
    must still pass the runner's allow-list and current registry checks.
    """

    def __init__(self, names: Iterable[str]) -> None:
        self._forward: dict[str, str] = {}
        self._reverse: dict[str, str] = {}
        for name in sorted(set(names)):
            wire = name
            if not _SAFE_NAME.fullmatch(name):
                readable = re.sub(r"[^A-Za-z0-9_]", "_", name.replace(":", "__"))
                suffix = hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]
                wire = f"{readable[:42]}__{suffix}"
            if wire in self._reverse and self._reverse[wire] != name:
                raise ValidationError("Provider tool name collision.", code="tool_name_collision")
            self._forward[name] = wire
            self._reverse[wire] = name

    def to_wire(self, name: str) -> str:
        return self._forward.get(name, name)

    def from_wire(self, name: str) -> str:
        return self._reverse.get(name, name)
