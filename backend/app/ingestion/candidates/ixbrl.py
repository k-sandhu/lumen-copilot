"""IXBRL candidate (#683), independently gated."""

from app.ingestion.candidates import Candidate

CANDIDATE = Candidate(
    "ixbrl",
    frozenset(["application/ixbrl+xml", "application/xhtml+xml", "text/html"]),
    frozenset(["ixbrl", "xhtml", "html", "xml"]),
)
