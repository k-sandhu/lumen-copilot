"""Notebook candidate (#684), never executing cells."""

from app.ingestion.candidates import Candidate

CANDIDATE = Candidate("notebook", frozenset({"application/x-ipynb+json"}), frozenset({"ipynb"}))
