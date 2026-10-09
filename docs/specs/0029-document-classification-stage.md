# Advisory classification stage — #692

ADR-0028 is authoritative. After extraction, commit a tenant/document work record
alongside the existing extraction checkpoint and audit; a separate Celery queue
and bounded recovery sweep consume it. Classification is never an upstream input
to chunking, embedding or index readiness. The source generation remains immutable.
Uploaded and connector documents share the existing ordinary ingestion seam.

Tenant policy is persisted, absent policy is disabled, and enabling requires an
admin, explicit provider/data-handling approval, model allow-list, tokenizer
identity, token/character/byte/time/retry/concurrency and spend limits. Existing
tenant provider routing resolves ephemeral credentials; a non-OpenRouter origin
cannot be used with the decisions endpoint. No chat selection enables this stage.
No production budget or review threshold is inferred. Model results always require
review until #694's owner-approved calibration artifacts exist.

The input fingerprint includes extraction checksum, taxonomy/rule/prompt versions
and tenant policy identity. A durable revision and worker lease fence computation
and publication. Save path, each conditional probability/confidence, evidence
facets/unknowns, requested/reported model, method(s), attempts/orders, actual or
unknown costs, extraction lineage, timestamps, retry reason and review state.
Failures retain completed levels, mark unclassified/incomplete, and retry only
typed transient errors with bounded exponential backoff. Overrides require current
visibility then document owner or tenant administrator; invisible/foreign -> 404,
visible non-owner member -> 403. Overrides are audited, revision fenced and never
overwritten by extraction, workers or bulk reclassification. Override reasons are
bounded relational data, excluded from audit. Denials use the canonical durable
denial context. Successful changes and content-free lifecycle audits commit together.

Budget reservations serialize on the tenant row in a separate tenant transaction.
Held ceilings plus known spend plus the new ceiling cannot exceed the approved
budget. Unknown outcome/accounting retains its ceiling. Per-attempt records store
usage, orders, method/model and cost. The canonical gateway emits mandatory durable
intent/terminal audit; failures prevent dispatch or result publication. Budget is
cumulative for this capability; reset/reconciliation is an owner-operated policy,
not an invented time window. Bulk reclassification uses keyset batches and schedules
only changed input/taxonomy/config records; no unbounded corpus materialization.

Acceptance: selected-branch cascade; batched yes/no facets; bounded full request
token counts; failure preserves levels; unknown spend remains held; atomic audit
rollback; idempotent input scheduling; stale worker/override races; same-tenant
read/override with foreign/invisible negatives; changed-only bulk selection; search
readiness succeeds independently of classification. Migration 0048 follows the
dependency's 0047; origin/main's latest revision was 0046 at allocation time.

Public fixtures are synthetic only. Live RLS/search and real-document egress remain
owner/evaluation gates. Media retains ADR-0023's transcription path; classification
of its transcript uses the same work record after transcription.
