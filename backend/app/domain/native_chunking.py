"""Native evidence and separate context, never extension objects."""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.domain.canonical import CanonicalDocument


@dataclass(frozen=True, slots=True)
class NativeChunk:
    ord: int
    text: str
    char_start: int
    char_end: int
    block_id: str
    context: str
    context_cell_indices: tuple[int, ...]
    token_count: int


@dataclass(frozen=True, slots=True)
class ChunkedDocument:
    document: CanonicalDocument
    chunks: tuple[NativeChunk, ...]

    @classmethod
    def from_json(cls, value: str) -> ChunkedDocument:
        raw = json.loads(value)
        document = CanonicalDocument.from_render_json(json.dumps(raw["rendered"]))
        chunks = tuple(
            NativeChunk(**(c | {"context_cell_indices": tuple(c["context_cell_indices"])}))
            for c in raw["chunks"]
        )
        for c in chunks:
            if document.rendered_text[c.char_start : c.char_end] != c.text:
                raise ValueError("native chunk evidence does not match retained source")
        return cls(document, chunks)
