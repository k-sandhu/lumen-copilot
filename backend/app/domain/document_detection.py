"""Immutable content-recognition result; no upload or permission authority."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DetectedDocument:
    format: str
    mime: str
    declared_mime: str | None
    declared_mismatch: bool
    evidence: str
    decoded_text: str | None
    encoding: str | None
    encoding_evidence: str | None
    decoding_errors: int

    @classmethod
    def from_json(cls, value: str) -> DetectedDocument:
        raw = json.loads(value)
        decoded = raw["decoded"]
        return cls(
            format=raw["format"],
            mime=raw["mime"],
            declared_mime=raw["declared_mime"],
            declared_mismatch=raw["declared_mismatch"],
            evidence=raw["evidence"],
            decoded_text=decoded["text"] if decoded else None,
            encoding=decoded["encoding"] if decoded else None,
            encoding_evidence=decoded["evidence"] if decoded else None,
            decoding_errors=decoded["errors"] if decoded else 0,
        )
