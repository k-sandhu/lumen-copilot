"""Retrieval adapter — the only issuer of vector + lexical search (CC-1 #45).

Single responsibility (ADR-0004 boundary table): hybrid search (pgvector
semantic similarity fused with Postgres full-text) followed by a cross-encoder
re-rank before context assembly. **Nobody else may issue a pgvector / full-text
search query.** The permission filter is applied **inside** this module, keyed
off the identity resolved in ``app.auth`` — there is no unfiltered retrieval
path (CC-1, mission filter #1, spec 0004 INV-2). Passages carry source + char
offsets through to the answer for citations (CC-11).

Public surface:

* :class:`RetrievalService` — permission-filtered hybrid ``search`` plus the
  three agent tools (``search_text`` / ``search_documents`` / ``get_document``)
  the chat runtime (#24) gives the LLM, named per the WS ``ChatToolCall``
  vocabulary.
* The result domain types live in :mod:`app.domain.retrieval`
  (:class:`RetrievedPassage`, :class:`DocumentMatch`, :class:`DocumentText`).

The SQL builders (:mod:`app.retrieval.queries`), the pure fusion/ranking
(:mod:`app.retrieval.fusion`), and the permission predicate
(:mod:`app.retrieval.permissions`) are internal collaborators of the service.
"""

from __future__ import annotations

from app.retrieval.permissions import AllowSet
from app.retrieval.service import RetrievalService

__all__ = [
    "AllowSet",
    "RetrievalService",
]
