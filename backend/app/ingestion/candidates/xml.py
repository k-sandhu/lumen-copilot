"""XML candidate (#683), independently gated."""

from app.ingestion.candidates import Candidate

CANDIDATE = Candidate("xml", frozenset(["application/xml", "text/xml"]), frozenset(["xml"]))
