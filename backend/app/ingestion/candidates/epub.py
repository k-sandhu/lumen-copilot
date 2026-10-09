"""EPUB admission and cutover stay independent of HTML (#681)."""

from app.ingestion.candidates import Candidate

CANDIDATE = Candidate("epub", frozenset({"application/epub+zip"}), frozenset({"epub"}))
