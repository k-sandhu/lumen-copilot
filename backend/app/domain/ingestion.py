"""Pure extraction provenance; offsets always index the rendered source."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

LocationKind = Literal["page", "slide", "sheet"]


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
