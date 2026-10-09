"""CSV and TSV stay independently gated (#675)."""

from app.ingestion.candidates import Candidate

CANDIDATE = Candidate(
    "csv", frozenset({"text/csv", "text/tab-separated-values"}), frozenset({"csv", "tsv", "text"})
)
