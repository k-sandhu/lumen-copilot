"""Versioned operational stage artifacts; no storage or authorization authority."""

from __future__ import annotations

from dataclasses import dataclass

STAGES = ("detect", "extract", "ocr", "normalize", "classify", "chunk", "embed", "index")


@dataclass(frozen=True, slots=True)
class StageOutput:
    stage: str
    fingerprint: str
    output_sha256: str
    payload_json: str


class StageOwnershipLost(Exception):
    """A cancelled, deleted or superseded attempt must cease publication."""


class StageOutputInvalid(Exception):
    """A stage cannot produce a bounded, finite JSON artifact."""
