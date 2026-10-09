# Resumable ingestion stages — #669

Status: proposed wire contract; ADR-0006 review/freeze precedes endpoint code.

Python owns orchestration, authorization, storage/network/model calls, audit and
generation activation. Persist detect, extract, normalize, classify, chunk, embed
and index stage outputs in the existing tenant database, keyed by source content
and each stage's own configuration/build plus upstream artifact checksum. Cache
data is operational, not a new historical-citation retention policy. A source or
parser change invalidates extraction; chunker/embedding-only changes reuse it.
Successful outputs/checksums commit together under the document's attempt fence.
Only one current output per document/stage is retained; changing a stage deletes
its stale downstream outputs in the same transaction. Restart clears all cached
stages atomically; document deletion cascades. Raw object bytes stay in storage.

Existing Python extraction/chunking remains authoritative. Normalize/classify
stages explicitly preserve that output when no approved implementation is enabled;
unknown classification is recorded as unknown, never invented. Native candidate
normalization/chunking remains separate and optional until #687's format approval.
Fingerprint actual returned embedding model/dimension; reject invalid vectors
before replacement. Index synchronization refreshes and rechecks the attempt before
ready. Retry always republishes index visibility for its new attempt; cached index
success cannot bypass readiness. Empty native text stays non-searchable under the
readiness/outcomes merge dependency (#629/#630). No old citation offset is retargeted.

## Proposed controls

`GET /api/v1/documents/{id}/processing` uses the existing retrieval visibility
predicate, including current mirrored ACL freshness. It returns state, lifecycle,
revision, current stage and completed stage names/fingerprints; never cached text,
embeddings, object keys, credentials, local paths or native exception payloads.
The read commits `document.viewed`. Foreign tenant or invisible document is 404.

`POST /api/v1/documents/{id}/processing` requires bearer authentication and body
`{action: cancel|resume|restart, expected_revision: nonnegative integer}`. First
require current visibility, then ownership or tenant admin role. Being an admin
does not grant document visibility; a visible non-owner member receives 403.
Unknown/extra/malformed fields return 422; stale revisions/illegal transitions 409.
Successful response is 200 with the durable processing projection. The transition
and `document.processing_controlled` audit event commit atomically. After-commit
enqueue uses the existing bounded broker seam; an outage leaves recoverable pending
work. The worker never trusts tenant/owner identities from body input.

Cancel: pending/processing -> lifecycle failed, explicit processing state cancelled,
safe `ingestion_cancelled` failure, revoke run token and advance attempt/revision.
Repeated cancel at the current revision is idempotent. Existing completed stage
outputs remain available for resume. Cancellation is cooperative at stage boundaries;
already running bounded provider/native work may finish but cannot commit/activate.
Resume: cancelled/failed -> pending, retain matching checkpoints, clear failure and
enqueue. Restart: any ordinary document state -> pending, revoke active lease,
advance attempt/revision and clear operational checkpoints before enqueue. Ready
cancel/resume is 409; restart is explicit. Media controls remain governed by
ADR-0023 and are 409 here, preserving paid transcription checkpoints.

Additive processing projection preserves the existing DocumentStatus enum.
Historical chunk/source-generation policy is unchanged; operational checkpoints
must never be used to silently resolve an old citation against new source text.

Acceptance: fault injection before/after every stage commit, embed/index failures
reuse extraction, settings-only invalidation, checksum corruption, cancel/retry
and stale-worker fencing, restart/delete cleanup, no duplicate active chunks or
orphan stage outputs, tenant/visibility/role/auth/input negatives, and audit rollback.
Offline SQLite/fakes validate application behavior. Live Postgres RLS/index refresh
and real platform parity remain unverified under this session's constraints.
