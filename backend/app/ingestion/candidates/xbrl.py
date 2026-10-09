"""XBRL candidate (#683), independently gated."""

from app.ingestion.candidates import Candidate

CANDIDATE = Candidate(
    "xbrl",
    frozenset(["application/xbrl+xml", "application/xml", "text/xml"]),
    frozenset(["xbrl", "xml"]),
)
