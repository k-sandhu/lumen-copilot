"""Document ingestion helpers — parse, chunk (pure, internal) — CC-5 (#21).

An **internal** package (ADR-0004 "Implementation notes"): not an external
adapter, so it gets no boundary-table row. It holds the dependency-localized
parsing and the pure chunking that the Celery ingestion task in ``app.tasks``
orchestrates. Nothing here does relational, object-store, or model I/O — those
are the owning adapters (``db/``, ``storage/``, ``llm/``) the task composes. The
task is the only thing that imports this package outside of tests.

* :mod:`app.ingestion.parsers` — MIME-typed extraction to plain text, each
  format behind a small helper so the parser libraries stay localized here.
* :mod:`app.ingestion.chunking` — overlapping, offset-carrying chunking; the
  ``char_start``/``char_end`` of every chunk index back into the parsed text
  exactly (passage-level citations, spec 0004 / the Citation contract).
"""

from app.ingestion.chunking import TextChunk, chunk_text
from app.ingestion.parsers import (
    SUPPORTED_MIME_TYPES,
    DocumentParseError,
    UnsupportedMimeTypeError,
    parse_document,
)

# Media helpers are intentionally imported by their concrete module from the
# worker.  Keeping them out of this legacy document-only export list prevents a
# broad import cycle while the task composes storage, DB, and LLM adapters.

__all__ = [
    "SUPPORTED_MIME_TYPES",
    "DocumentParseError",
    "TextChunk",
    "UnsupportedMimeTypeError",
    "chunk_text",
    "parse_document",
]
