"""``app/search/`` — the dedicated text-search index boundary (ADR-0010).

The ONE module that talks to OpenSearch, the single retrieval store (BM25
lexical + kNN vectors). Everything engine-shaped lives behind it:
:class:`OpenSearchStore` (schema, writes, the hybrid query — over plain HTTP,
no vendor SDK, per ADR-0004) and :class:`SearchAllowFilter` (the INV-1/INV-2
permission predicate every query must carry; an unfiltered query is
unrepresentable). Only the ``retrieval/`` chokepoint issues reads (ADR-0010
§3); the ingestion write path (Celery) issues writes. Callers see domain types
only — never an OpenSearch response shape.
"""

from app.search.filters import SearchAllowFilter
from app.search.store import IndexedChunk, OpenSearchStore, SearchHit

__all__ = [
    "IndexedChunk",
    "OpenSearchStore",
    "SearchAllowFilter",
    "SearchHit",
]
