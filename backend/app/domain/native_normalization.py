"""Immutable normalized representations with explicit derived lineage."""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.domain.canonical import CanonicalDocument


@dataclass(frozen=True, slots=True)
class NormalizedBlock:
    block_id: str
    text: str
    origin: str


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    document: CanonicalDocument
    blocks: tuple[NormalizedBlock, ...]
    diagnostics_json: str

    @classmethod
    def from_json(cls, value: str) -> NormalizedDocument:
        raw = json.loads(value)
        return cls(
            CanonicalDocument.from_render_json(json.dumps(raw["rendered"])),
            tuple(NormalizedBlock(**block) for block in raw["normalized_blocks"]),
            json.dumps(raw["diagnostics"], ensure_ascii=False, sort_keys=True),
        )
