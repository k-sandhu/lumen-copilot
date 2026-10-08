"""Inert runtime configuration and computation diagnostics."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeBudget:
    max_input_bytes: int = 32 * 1024 * 1024
    max_memory_bytes: int = 128 * 1024 * 1024
    max_output_chars: int = 2_000_000
    max_work_units: int = 100_000
    timeout_ms: int = 30_000


@dataclass(frozen=True, slots=True)
class ComputedUnits:
    units: tuple[str, ...]
    peak_accounted_bytes: int
    work_units: int
    output_chars: int
    source_input_bytes: int

    @classmethod
    def from_json(cls, value: str) -> ComputedUnits:
        raw = json.loads(value)
        return cls(tuple(raw["units"]), **raw["diagnostics"])
