"""Independent synthetic fidelity checks; extraction failures stay in denominators."""

from __future__ import annotations

import re
from dataclasses import dataclass

_TOKEN = re.compile(r"[+\-−]?\d+(?:[.,]\d+)*|[^\W\d_]+|[^\s]", re.UNICODE)


@dataclass(frozen=True)
class Gold:
    facts: tuple[str, ...] = ()
    associations: tuple[tuple[str, ...], ...] = ()
    order: tuple[str, ...] = ()
    native_regions: int = 0
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Score:
    fact_coverage: float
    table_association: float
    reading_order: float
    exact_offsets: float
    cell_header_association: float
    native_provenance: float
    outcome: str


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(token.replace("−", "-") for token in _TOKEN.findall(text))


def _find(haystack: tuple[str, ...], needle: tuple[str, ...]) -> int:
    if not needle:
        return -1
    return next(
        (
            i
            for i in range(len(haystack) - len(needle) + 1)
            if haystack[i : i + len(needle)] == needle
        ),
        -1,
    )


def evaluate(
    text: str,
    gold: Gold,
    *,
    spans: tuple[tuple[int, int, str], ...] = (),
    matched_regions: int = 0,
    cell_headers: tuple[tuple[str, str], ...] = (),
    outcome: str = "indexed",
) -> Score:
    tokens = _tokens(text)
    facts = sum(_find(tokens, _tokens(fact)) >= 0 for fact in gold.facts)
    lines = [_tokens(line) for line in text.splitlines()]
    groups = sum(
        any(all(_find(line, _tokens(fact)) >= 0 for fact in group) for line in lines)
        for group in gold.associations
    )
    positions = [_find(tokens, _tokens(anchor)) for anchor in gold.order]
    pairs = list(zip(positions, positions[1:], strict=False))
    order = sum(0 <= a < b for a, b in pairs) / len(pairs) if pairs else 1.0
    valid = all(
        0 <= start <= end <= len(text) and text[start:end] == value for start, end, value in spans
    )
    successful = outcome in {"indexed", "empty"}
    return Score(
        facts / len(gold.facts) if gold.facts else float(successful),
        groups / len(gold.associations) if gold.associations else float(successful),
        order if successful else 0.0,
        float(valid and successful),
        sum(pair in cell_headers for pair in gold.headers) / len(gold.headers)
        if gold.headers
        else 1.0,
        min(matched_regions / gold.native_regions, 1.0) if gold.native_regions else 1.0,
        outcome,
    )


def require_fidelity(score: Score, *, format_name: str, require_provenance: bool = False) -> None:
    values = (
        score.fact_coverage,
        score.table_association,
        score.reading_order,
        score.exact_offsets,
        score.cell_header_association,
    )
    if (
        score.outcome not in {"indexed", "empty"}
        or min(values) < 1.0
        or (require_provenance and score.native_provenance < 1.0)
    ):
        raise ValueError(f"fidelity gate failed for {format_name}")
