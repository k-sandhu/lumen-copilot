# Immutable re-ingestion generation proposal — #687

ADR-0025 requires retention, migration/backfill and historical-source policy owner
decisions before implementation. This proposal does not activate those behaviors.

Proposed pilot policy: retain a generation while any historical citation references
it; no automatic TTL. Document deletion/revocation applies to historical access.
Tenant deletion removes retained generations under the existing deletion policy.
Storage quota and explicit administrative retirement need an owner-approved bound;
retired evidence reports unavailable, never silently redirects a citation.

A document gets an immutable generation ID recording original-byte checksum and
versioned object reference, canonical source/map, parser/renderer identity and
immutable chunks. Active generation switches atomically only after embedding and
refreshed indexing succeeds. Rollback selects a retained generation. Historical
citations always resolve their generation plus current permission checks.

Before replacing any existing chunks, backfill a legacy generation from the actual
stored evidence and bind every old citation to it. Unknown original/parser identity
stays unknown. Validate counts/checksums/citation slices; abort on unmappable rows.
Never infer a historical source from newly parsed bytes. Version/copy mutable
connector originals before extracting. Re-ingestion creates a new generation
from stored originals for a bounded document/collection/tenant cursor page.

First delivery uses token-authenticated administrator CLI with per-document access
checks, preview by default, explicit execute and existing governed audit actions.
No HTTP/WS shape changes are proposed in this delivery. If a historical-source or
re-ingestion API is requested, ADR-0006 contract review must precede its code.

Owner approval required: retained-until-unreferenced pilot policy; quota/retirement
bound; legacy backfill/atomic active-generation design; unavailable historical
source behavior. Until approved, this issue provides original-byte shadow replay,
read-only inventory and an execution gate; it never calls chunk replacement or
ordinary ingestion enqueue for re-ingestion.
