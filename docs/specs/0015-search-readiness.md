# Spec 0015 — Search-visible document readiness

Tracking: [#626](https://github.com/k-sandhu/lumen-copilot/issues/626).

`ready` means at least one chunk has been synchronized with the single retrieval
store and that synchronization requested refresh visibility. Persist chunks while
the document is `processing`; request refreshed deletion of the prior chunk IDs
and refreshed bulk upsert, then commit `ready` in a separate tenant transaction.
Refresh acknowledges visibility, not merely acceptance of an asynchronous write
([OpenSearch Bulk API](https://docs.opensearch.org/latest/api-reference/document-apis/bulk/)).
No actual engine I/O or database access belongs in routers.

Any deletion/upsert/refresh fault is retryable through the existing whole-pipeline
retry/backoff path. Never advance ready on a rejected/partial bulk write. Exhausted
retries use the existing failed terminal state. A crash between successful sync
and activation leaves processing, which is truthful and converges on re-drive.
Empty extraction clears PostgreSQL chunks and refreshed index entries, and records
failed with an actionable no-native-text message; it never advertises searchability.

Acceptance: observe committed processing from inside a fake index write; ready
appears only after successful refreshed synchronization. Inject deletion and
upsert faults, assert no ready state, then retry successfully. Verify empty
re-ingestion clears prior entries. Tenant/permission predicates and existing audit
commits remain unchanged. Engine adapter tests retain partial-bulk rejection.

This change is independent of extraction outcomes (#624), with additive changes
to the same phases. When combined, publish successful outcome and ready together
after synchronization. It requires no parser or embedding changes and no migration.
Search reindex retains its caller-selected refresh behavior; ingestion explicitly
opts into refresh. Refresh costs extra engine work per ingested document, which
must be measured under realistic bulk ingest load before enabling high throughput.

Deferred: concurrent ingestion generations/leases, atomic alias swaps, and an
automatic sweep for abandoned processing attempts. Ready is an activation-time
guarantee, not a permanent promise that an external engine never becomes unavailable.
No historical offsets or chunk text change; existing ready documents are not
automatically re-ingested or re-indexed.
