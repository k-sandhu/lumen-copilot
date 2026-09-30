# Task-shaped corpus discovery and reading

Tracking: [#627](https://github.com/k-sandhu/lumen-copilot/issues/627).
Depends on conversation handles (#436) and complete passages (#611).

New ad-hoc conversations offer `search_passages`, `find_documents` and
`read_document`. Explicit saved tool lists retain their original names; legacy
tools remain registered. A canonical or legacy deny applies to the complete
capability family. No immutable assistant configuration is rewritten.

Discovery searches literal substrings in title, filename, source path and public
document metadata, never connector configuration or credentials. Connector
sync preserves its fetched title, URL and modified timestamp; uploads fall back
to their filename. Existing rows need a resync for missing source metadata.
Discovery returns a permission-filtered page before sorting and pagination,
with title/creation/source-modified ascending or descending and stable document-ID ties.
Opaque cursors bind the requester, tenant, query, filters and sort. Each page
rechecks current ACLs, ownership, grants and group membership. Limits are 1–50, default 10.
Unknown modified timestamps sort last and never satisfy a bounded modified-date
filter; source dates are not replaced with ingestion dates.

All structured filters intersect with answer scope and current permissions:
source kind, MIME type, creation or source-modified dates, collection IDs and document IDs. An empty
intersection remains empty. Passage filtering uses bounded relational discovery
before the existing search engine; too many eligible documents require narrower
filters rather than silent truncation.

`read_document` accepts a document UUID or D handle, or reads around an S handle.
Handles are navigation identities, never authorization. Around reads rehydrate
the original passage and reject changed evidence. Reads select complete chunks
intersecting the requested character range; returned boundaries may extend to
chunk boundaries. The default passage limit is 5 (maximum 20), reduced by the
context engine. Total length, returned range and explicit continuation identify
omitted chunks. Continuation advances past already returned chunk ends, while
preserving overlapping neighboring chunks. Missing and forbidden documents share
the same honest empty/not-found result without exposing metadata.
If a passage limit splits chunks with the same end offset, the tool asks for a
larger passage budget or narrower range rather than returning a continuation
that would skip evidence. Typed range/cursor errors retain their safe recovery
instructions. Every canonical and legacy retrieval read refreshes membership,
because even one answer can span a group revocation.

Acceptance tests cover real metadata discovery, stable paging, literal wildcard
characters, filter intersections, whole overlapping chunks, exact continuation,
unknown/conversation handles, revoked permission and legacy callable names.
