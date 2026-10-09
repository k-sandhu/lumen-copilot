"""Saved web-page candidate (#679)."""

from app.ingestion.candidates import Candidate

CANDIDATE = Candidate(
    "html",
    frozenset({"text/html", "application/xhtml+xml", "multipart/related"}),
    frozenset({"html", "xhtml", "mhtml", "xml"}),
)
