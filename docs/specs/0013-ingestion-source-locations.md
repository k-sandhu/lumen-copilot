# Spec 0013 — Exact source-part provenance

Tracking: [#621](https://github.com/k-sandhu/lumen-copilot/issues/621).

Retain a source map beside the exact extracted text. Each entry has `kind`
(`page`, `slide`, `sheet`), source-derived `name`, one-based `number`, and
half-open `char_start` / `char_end` into that string. Blank parts retain
zero-width spans. Separators may lie outside mapped spans; they are rendering
delimiters, not independent evidence. Plain text, Markdown and DOCX have no
native page map and do not receive invented page numbers.

`parse_document_with_locations` returns text plus the map. `parse_document`
keeps its string interface and identical rendering. Format helpers collect
maps while extracting; there is no second lossy parse. Chunk text stays exactly
`source[char_start:char_end]`; each chunk records all nonempty intersecting
source parts, so a chunk crossing a page boundary can name both pages.

Persist text and map on the document and locations on its chunks in the same
tenant-scoped transaction. Search indexing/backfill carries locations as
non-analyzed provenance with an additive `source_locations` object mapping
(`enabled: false`). It changes neither ranking nor permission filters.
Permission-filtered hydration passes locations through
to retrieved passages, enabling citation consumers to name the source part.
No permission predicate, tool policy or citation display changes here.

Contract-first: add optional `Document.source_locations` (default empty array)
and a reusable `SourceLocation` schema. GET/list retain existing bearer,
tenant and owner-or-grant enforcement and audit behavior; provenance never
widens visibility. The existing text endpoint prefers retained extraction text,
falling back to chunk reconstruction for old documents. Regenerate the client
and reconcile the frontend mirror; no generated output is committed.

Acceptance: multiple PDF pages/slides/sheets including blanks have exact spans;
chunks crossing boundaries carry all locations with exact text; persistence,
index projection and retrieval preserve maps; forbidden documents disclose no
text or locations. Invalid/corrupt bytes remain typed parse errors.

Deployment backfills nothing: old documents have null retained text and empty
maps, which means unknown location, not page 1. Operators must re-ingest retained
bytes or upload a new document for maps. Search reindex copies stored chunks
and locations, without reparsing. Re-ingestion replaces chunk IDs under current
semantics. Stored historical offsets/passages are not rewritten; previous source
generations are not retained, so explicit successful re-ingestion may leave old
citations stale or unresolved. Historical resolution remains a known limitation,
not a guarantee that an old offset can address a newly rendered source.

Deferred: source versions, page images, bounding boxes, cell/block identities,
stable historical citation identity, canonical structured parsing and UI for
locations. Retained text adds relational storage; long chunks may still cross
parts. Rendering improvements in separate parser PRs must preserve map offsets
when merged rather than replacing the format helpers wholesale.
