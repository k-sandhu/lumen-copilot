# Spec 0015 — Search-visible document readiness

Tracking: [#626](https://github.com/k-sandhu/lumen-copilot/issues/626).

`ready` means at least one chunk has been synchronized with the single retrieval
store and that synchronization acknowledged refresh visibility. Preserve the
attempt-scoped publication barrier from #605 / ADR-0010: persist chunks while
`processing`, accept every bounded current-generation bulk, acknowledge one
index-wide refresh, retire only older generations with refreshed deletion, then
commit `ready` with the expected-attempt CAS in a separate tenant transaction.
Refresh acknowledges visibility, not merely acceptance of an asynchronous write
([OpenSearch Bulk API](https://docs.opensearch.org/latest/api-reference/document-apis/bulk/)).
No actual engine I/O or database access belongs in routers.

HTTP 200 alone is insufficient. Retain the shared adapter completion validator:
delete-by-query must not time out, report failures or version conflicts; every
bulk item's shards and the explicit refresh must acknowledge complete execution.
Any deletion/upsert/refresh fault is retryable through the existing whole-pipeline
retry/backoff path. Never advance ready on a rejected/partial bulk write. The
existing finalizer records failed attempts safely before retry; exhausted retries
use the failed terminal state. A crash between successful sync
and activation leaves processing, which is truthful and converges on re-drive.
Empty extraction replaces PostgreSQL chunks with an empty set under the attempt
fence, clears that generation and only older index generations with refreshed
deletion, then records failed (`no_native_text`) through the expected-attempt
finalizer with an actionable message. Cleanup failures remain retryable and never
advertise searchability. A superseded attempt cannot fail a newer generation.

Acceptance: observe committed processing from inside a fake index write; ready
appears only after successful refreshed synchronization. Inject deletion and
upsert faults, assert no ready state, then retry successfully. Exercise incomplete
HTTP-200 deletion, bulk-item shard and refresh responses through the real adapter,
including both empty-reingestion cleanup requests. Verify empty re-ingestion clears
prior entries before terminal failure, and finalization cannot overwrite a newer
attempt. Tenant/permission predicates and existing audit commits remain unchanged.

This change is independent of extraction outcomes (#624), with additive changes
to the same phases. When combined, publish successful outcome and ready together
after synchronization. It requires no parser or embedding changes and no migration.
Search reindex retains its caller-selected refresh behavior; ingestion explicitly
opts into publication visibility and refreshed generation cleanup. Refresh costs
extra engine work per ingested document, which
must be measured under realistic bulk ingest load before enabling high throughput.

Generation/lease fencing and embedding fingerprints already exist on main and
are preserved. Deferred: atomic alias swaps and an automatic sweep for abandoned
processing attempts. Ready is an activation-time
guarantee, not a permanent promise that an external engine never becomes unavailable.
No historical offsets or chunk text change; existing ready documents are not
automatically re-ingested or re-indexed.
