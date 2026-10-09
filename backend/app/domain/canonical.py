"""Immutable Python views of the validated canonical computation contract v1."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BoundingBox:
    x0: float
    y0: float
    x1: float
    y1: float
    unit: str
    origin: str


@dataclass(frozen=True, slots=True)
class CellRange:
    row_start: int
    row_end: int
    column_start: int
    column_end: int


@dataclass(frozen=True, slots=True)
class SourceRegion:
    kind: str | None
    number: int | None
    name: str | None
    cell_range: CellRange | None
    bbox: BoundingBox | None
    origin: str


def _region(raw: dict[str, Any]) -> SourceRegion:
    return SourceRegion(
        kind=raw["kind"],
        number=raw["number"],
        name=raw["name"],
        cell_range=CellRange(**raw["cell_range"]) if raw["cell_range"] else None,
        bbox=BoundingBox(**raw["bbox"]) if raw["bbox"] else None,
        origin=raw["origin"],
    )


@dataclass(frozen=True, slots=True)
class SourcePart:
    kind: str
    name: str
    number: int
    char_start: int
    char_end: int


@dataclass(frozen=True, slots=True)
class CanonicalCell:
    row: int
    column: int
    row_span: int
    column_span: int
    text: str
    header_role: str
    role_origin: str
    formula: str | None
    cached_value_json: str | None
    cache_freshness: str | None
    format: str | None
    unit: str | None
    regions: tuple[SourceRegion, ...]


@dataclass(frozen=True, slots=True)
class CanonicalTable:
    rows: int
    columns: int
    cells: tuple[CanonicalCell, ...]
    caption: str | None


def _table(raw: dict[str, Any] | None) -> CanonicalTable | None:
    if raw is None:
        return None
    cells = tuple(
        CanonicalCell(
            row=c["row"],
            column=c["column"],
            row_span=c["row_span"],
            column_span=c["column_span"],
            text=c["text"],
            header_role=c["header_role"],
            role_origin=c["role_origin"],
            formula=c["formula"],
            cached_value_json=json.dumps(c["cached_value"])
            if c["cached_value"] is not None
            else None,
            cache_freshness=c["cache_freshness"],
            format=c["format"],
            unit=c["unit"],
            regions=tuple(_region(r) for r in c["regions"]),
        )
        for c in raw["cells"]
    )
    return CanonicalTable(raw["rows"], raw["columns"], cells, raw["caption"])


@dataclass(frozen=True, slots=True)
class CanonicalBlock:
    id: str
    kind: str
    text: str
    parent_id: str | None
    heading_level: int | None
    heading_path: tuple[str, ...]
    origin: str
    regions: tuple[SourceRegion, ...]
    table: CanonicalTable | None


@dataclass(frozen=True, slots=True)
class BlockSpan:
    block_id: str
    char_start: int
    char_end: int


@dataclass(frozen=True, slots=True)
class CanonicalDocument:
    schema_version: int
    renderer_version: int
    blocks: tuple[CanonicalBlock, ...]
    source_parts: tuple[SourcePart, ...]
    rendered_text: str
    spans: tuple[BlockSpan, ...]
    generation_json: str
    document_json: str

    @classmethod
    def from_render_json(cls, value: str) -> CanonicalDocument:
        """Build an immutable view; input is the native validator's output."""
        raw = json.loads(value)
        doc = raw["document"]
        blocks = tuple(
            CanonicalBlock(
                id=b["id"],
                kind=b["kind"],
                text=b["text"],
                parent_id=b["parent_id"],
                heading_level=b["heading_level"],
                heading_path=tuple(b["heading_path"]),
                origin=b["origin"],
                regions=tuple(_region(r) for r in b["regions"]),
                table=_table(b["table"]),
            )
            for b in doc["blocks"]
        )
        return cls(
            schema_version=doc["schema_version"],
            renderer_version=doc["renderer_version"],
            blocks=blocks,
            source_parts=tuple(SourcePart(**p) for p in doc["source_parts"]),
            rendered_text=raw["rendered_text"],
            spans=tuple(BlockSpan(**s) for s in raw["spans"]),
            generation_json=json.dumps(doc["generation"], ensure_ascii=False, sort_keys=True),
            document_json=json.dumps(doc, ensure_ascii=False, sort_keys=True),
        )
