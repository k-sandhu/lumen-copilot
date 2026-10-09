"""Immutable bounded classification evidence; no complete source payload."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ClassificationFeatures:
    payload_json: str

    @classmethod
    def from_json(cls, value: str) -> ClassificationFeatures:
        raw = json.loads(value)
        if raw.get("schema_version") != 1 or "rendered_text" in raw:
            raise ValueError("invalid classification evidence")
        return cls(json.dumps(raw, ensure_ascii=False, sort_keys=True))

    def payload(self) -> dict[str, Any]:
        return dict(json.loads(self.payload_json))
