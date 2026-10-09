"""JSON candidate (#683), independently gated."""

from app.ingestion.candidates import Candidate

CANDIDATE = Candidate("json", frozenset(["application/json"]), frozenset(["json"]))
