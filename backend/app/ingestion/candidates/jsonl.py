"""JSONL candidate (#683), independently gated."""

from app.ingestion.candidates import Candidate

CANDIDATE = Candidate(
    "jsonl", frozenset(["application/x-ndjson", "application/jsonl"]), frozenset(["jsonl", "json"])
)
