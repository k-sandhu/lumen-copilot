# Spec 0027 — Task-shaped corpus discovery and reading

Tracking: [#627](https://github.com/k-sandhu/lumen-copilot/issues/627).
Depends on conversation handles (#436), complete passages (#611), and the
[tool-use policy](0026-tool-use-policy.md) (#635). The default provider prompt
names the offered canonical tools; it must not direct a new conversation to
unavailable legacy tools. Explicit saved legacy catalogs remain supported.

New ad-hoc conversations offer `search_passages`, `find_documents` and
`read_document`. Explicit saved tool lists retain their original names; legacy
tools remain registered. A canonical or legacy deny applies to the complete
capability family. No immutable assistant configuration is rewritten.

Discovery searches literal substrings in title, filename, source path and public
document metadata, never connector configuration or credentials. Connector
sync preserves its fetched title, URL and modified timestamp; uploads fall back
to their filename. Migration `0049_discovery_metadata` adds nullable public
metadata fields after `0048_evidence_handles`; existing rows retain
filename fallback and unknown source dates until resync. The migration chain
has one head, `0049_discovery_metadata`, on the integrated handle/policy stack.
Discovery returns a permission-filtered page before sorting and pagination,
with title/creation/source-modified ascending or descending and stable document-ID ties.
Empty titles fall back to filename. Cursor keys come from the database-evaluated
sort expression, including its case normalization, rather than Python lowercasing.
Opaque cursors bind the requester, tenant, query, filters and sort. Each page
rechecks current ACLs, ownership, grants and group membership. Limits are 1–50, default 10.
Unknown modified timestamps sort last and never satisfy a bounded modified-date
filter; source dates are not replaced with ingestion dates.

Each `search_passages` hit names the tokens `read_document` accepts (#772): its
header is `[S3] name (document D2, chars a-b)`, followed by media provenance
when present. The D handle comes from the same conversation registry as
`find_documents`, and a second hit in the same document reuses it. Compaction
re-embeds this exact block. D handles navigate and never support a claim.
A read `handle` that names no available document or passage gets one
`tool_not_found` reply. That covers a filename, an unknown or foreign handle or
UUID, a revoked document, and changed evidence. The reply says `handle` takes a
D or S handle, names only the offered tools that show one, and discloses nothing
about the value.

All structured filters intersect with answer scope and current permissions:
source kind, MIME type, creation or source-modified dates, collection IDs and document IDs. An empty
intersection remains empty. Passage filtering uses bounded relational discovery
before the existing search engine; too many eligible documents require narrower
filters rather than silent truncation.

`read_document` takes one required `handle` (#771). A D handle or document UUID
reads the document's character range (`start`/`end`). An S handle reads 1200
characters either side of that passage and ignores any range, because some
models fill every optional field. There is no pair of mutually exclusive
parameters. Decision, from notebook 08 section 9 (2026-10-16):
- **Chosen: one `handle` parameter (option a).**
- **Rejected: a separate `read_around(passage)` tool (option b).**
- Why not the old pair: under `document`/`around`, 21–38% of identifier calls
  failed for three models, and on plain schemas gpt-6-luna sent both on every
  read. Actionable errors alone did not lower that (28–40%).
- Why (a) over (b): both measured 0% identifier failures, with 91–95% accuracy
  for three models and 91% for gpt-6-luna in Lumen's wire format. A single
  parameter keeps the three-tool canonical family unchanged, along with its
  legacy alias, policy resolution and default catalog.
- No alias is kept for the old `document`/`around` arguments, since one would
  bring back the exclusive pair. The runner rejects them as unknown properties
  before any I/O.
Handles are navigation identities, never authorization. Around reads rehydrate
the original passage and reject changed evidence, including changed media
timestamps, transcript segment and speaker provenance even when text is unchanged.
This check does not allocate new conversation handles. Reads select complete chunks
intersecting the requested character range; returned boundaries may extend to
chunk boundaries. The default passage limit is 5 (maximum 20), reduced by the
context engine. Total length, returned range and explicit continuation identify
omitted chunks. Continuation advances past already returned chunk ends, while
preserving overlapping neighboring chunks. Missing and forbidden documents share
the same honest empty/not-found result without exposing metadata.
Range boundaries and continuation instructions remain in the actual provider
tool transcript alongside the complete evidence handles. Range hydration uses
the same validated media provenance and source-part locations as final citation read-back.
Canonical reads retain immutable answer-level document dependencies for permissioned recall. Point, length,
chunk and hydration queries require READY publication; pending/failed documents
cannot expose retained chunks.
If a passage limit splits chunks with the same end offset, or a returned whole
chunk reaches/passes the requested end while overlapping chunks remain, the tool asks for a
larger passage budget or narrower range rather than returning a continuation
that would skip evidence. Typed range/cursor errors retain their safe recovery
instructions. Every canonical and legacy retrieval read refreshes membership,
because even one answer can span a group revocation.
Both declared tool families emit `retrieval.query` alongside their generic tool
events. Audited reads and uncited refusals commit through the chat route;
independent read-back transactions verify those events and zero citations.

Acceptance tests cover real metadata discovery, stable paging, literal wildcard
characters, filter intersections, whole overlapping chunks, exact continuation,
unknown/conversation handles, revoked permission, committed canonical-tool
retrieval audits, changed media provenance on around-passage reads,
and legacy callable names. The canonical context-budget route
also verifies that the actual provider prompt names the offered corpus tools,
while refusing oversized evidence before a second provider request.
