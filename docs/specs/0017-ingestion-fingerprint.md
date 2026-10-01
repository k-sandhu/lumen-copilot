# Spec 0017 — Reproducible retained ingestion generations

Tracking: [#632](https://github.com/k-sandhu/lumen-copilot/issues/632). Depends on
#621 for retained source and metadata; independent of outcomes and diagnostics.

Each newly retained extraction records fingerprint schema version 1, SHA-256 of
the original bytes, native parser version and implementation SHA-256, installed
parser distribution versions, chunker version and configured character size and
overlap, and the actual returned embedding model and vector dimension. Hash parser
and chunker implementation source without paths so independent parser/overlap
changes are identifiable in either merge order. No credentials or source text
belong in the fingerprint. Native text has no third-party parser dependency.

Require one nonblank actual model and one positive dimension across all returned
embeddings, with finite vector elements. Invalid or inconsistent results raise a
typed ingestion error before any replacement. This validates provenance only;
embedding compatibility policy and native-dimension enforcement remain #346.
An empty extraction records absent actual model/dimension, never the configured
model as an observed result. Legacy fingerprints remain unknown.

Persist the fingerprint in the same tenant-scoped transaction as retained source,
location map and replacement chunks. A failed fetch/parse/embed attempt preserves
the prior generation fingerprint and citation slices. Index synchronization
failure does not erase the prepared generation fingerprint and does not make it
a search-readiness assertion. Preserve other ingestion metadata keys.

Expose an optional nullable `ingestion_fingerprint` on the existing permissioned
Document contract. Actual embedding fields may be absent/null for empty text.
No new administrator bypass, source access path, provider call or dependency.

Acceptance: deterministic reruns; source/settings/model/dimension changes are
observable; empty extraction and legacy absence are explicit; malformed/mixed
embedding metadata fails before replacement; failed re-ingestion preserves the
old fingerprint/source/chunks; cross-tenant access remains not found.

Original-byte re-ingestion regenerates fingerprints and changes current chunks
and offsets atomically. Search reindex copies persisted chunks and does not
reparse or create a fingerprint. Historical citations retain their original
stored passage/offsets; this change does not version old source generations or
silently reinterpret a stored offset against a new extraction.
