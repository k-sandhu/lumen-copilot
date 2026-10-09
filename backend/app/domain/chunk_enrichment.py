"""Derived context has lineage, never evidence offsets of its own."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.native_chunking import ChunkedDocument


@dataclass(frozen=True, slots=True)
class ContextInput:
    text: str
    char_start: int
    char_end: int
    structural_context: str = ""
    block_id: str | None = None
    cell_indices: tuple[int, ...] = ()


def native_context_inputs(document: ChunkedDocument) -> tuple[ContextInput, ...]:
    """Preserve the chunker's breadcrumb/header/unit context as separate lineage."""
    return tuple(
        ContextInput(
            c.text, c.char_start, c.char_end, c.context, c.block_id, c.context_cell_indices
        )
        for c in document.chunks
    )


@dataclass(frozen=True, slots=True)
class ChunkContext:
    deterministic_text: str
    generated_text: str | None
    fingerprint: str
    block_id: str | None
    cell_indices: tuple[int, ...]

    @property
    def origin(self) -> str:
        return "generated" if self.generated_text is not None else "deterministic"

    @property
    def metadata(self) -> dict[str, object]:
        return {
            "version": 1,
            "block_id": self.block_id,
            "cell_indices": list(self.cell_indices),
            "generated_origin": "model_generated" if self.generated_text else None,
        }
