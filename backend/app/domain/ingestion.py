"""Pure extraction provenance; offsets always index the rendered source."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

LocationKind = Literal["page", "slide", "sheet"]
TableProbe = Literal["native_tables", "sheet_cells", "unavailable"]


@dataclass(frozen=True, slots=True)
class ExtractionDiagnostics:
    character_count: int
    replacement_characters: int
    suspicious_controls: int
    source_part_kind: LocationKind | None
    total_parts: int | None
    parts_with_text: int | None
    blank_parts: tuple[int, ...]
    table_probe: TableProbe
    table_regions: int | None
    table_cells: int | None
    missing_table_cells: int | None
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        counts = (
            self.character_count,
            self.replacement_characters,
            self.suspicious_controls,
            self.total_parts,
            self.parts_with_text,
            self.table_regions,
            self.table_cells,
            self.missing_table_cells,
        )
        if any(value is not None and (type(value) is not int or value < 0) for value in counts):
            raise ValueError("invalid extraction diagnostic count")
        if self.source_part_kind not in (None, "page", "slide", "sheet"):
            raise ValueError("invalid diagnostic source part kind")
        if self.table_probe not in ("native_tables", "sheet_cells", "unavailable"):
            raise ValueError("invalid diagnostic table probe")
        if any(type(value) is not int or value < 1 for value in self.blank_parts):
            raise ValueError("invalid blank source part")
        if any(not isinstance(value, str) for value in self.warnings):
            raise ValueError("invalid extraction warning")

    def to_dict(self) -> dict[str, object]:
        return {
            "character_count": self.character_count,
            "replacement_characters": self.replacement_characters,
            "suspicious_controls": self.suspicious_controls,
            "source_part_kind": self.source_part_kind,
            "total_parts": self.total_parts,
            "parts_with_text": self.parts_with_text,
            "blank_parts": list(self.blank_parts),
            "table_probe": self.table_probe,
            "table_regions": self.table_regions,
            "table_cells": self.table_cells,
            "missing_table_cells": self.missing_table_cells,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ExtractionDiagnostics:
        return cls(
            character_count=cast(int, value["character_count"]),
            replacement_characters=cast(int, value["replacement_characters"]),
            suspicious_controls=cast(int, value["suspicious_controls"]),
            source_part_kind=cast(LocationKind | None, value["source_part_kind"]),
            total_parts=cast(int | None, value["total_parts"]),
            parts_with_text=cast(int | None, value["parts_with_text"]),
            blank_parts=tuple(cast(list[int], value["blank_parts"])),
            table_probe=cast(TableProbe, value["table_probe"]),
            table_regions=cast(int | None, value["table_regions"]),
            table_cells=cast(int | None, value["table_cells"]),
            missing_table_cells=cast(int | None, value["missing_table_cells"]),
            warnings=tuple(cast(list[str], value["warnings"])),
        )


@dataclass(frozen=True, slots=True)
class SourceLocation:
    kind: LocationKind
    name: str
    number: int
    char_start: int
    char_end: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or any(
            type(value) is not int for value in (self.number, self.char_start, self.char_end)
        ):
            raise ValueError("invalid source location fields")
        if self.kind not in ("page", "slide", "sheet") or self.number < 1:
            raise ValueError("invalid source location kind or number")
        if not 0 <= self.char_start <= self.char_end:
            raise ValueError("invalid source location span")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "number": self.number,
            "char_start": self.char_start,
            "char_end": self.char_end,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SourceLocation:
        return cls(
            kind=cast(LocationKind, value["kind"]),
            name=cast(str, value["name"]),
            number=cast(int, value["number"]),
            char_start=cast(int, value["char_start"]),
            char_end=cast(int, value["char_end"]),
        )


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    text: str
    locations: tuple[SourceLocation, ...] = ()

    def __post_init__(self) -> None:
        if any(location.char_end > len(self.text) for location in self.locations):
            raise ValueError("source location extends past extracted text")

    def locations_for_span(self, start: int, end: int) -> tuple[SourceLocation, ...]:
        if not 0 <= start <= end <= len(self.text):
            raise ValueError("span outside extracted text")
        if start == end:
            return ()
        return tuple(
            location
            for location in self.locations
            if location.char_start < location.char_end
            and location.char_start < end
            and start < location.char_end
        )
