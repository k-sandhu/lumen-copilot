# Contextual chunk enrichment — #686

Sponsor-directed implementation under ADR-0025/0027 and #667. No API/WS change.

Every newly ingested ordinary chunk has deterministic context in a separate
indexed field: supplied document title, supplied type or explicit unknown,
declared source format, and available canonical breadcrumbs/table headers/units
from #667. No classification is inferred; callers may supply an already computed
type. Context retains canonical block/cell lineage without citation offsets.
Python-only ingestion supplies the metadata it actually knows; absent structure
remains absent. Enrichment never mutates source text, chunk text or evidence spans.

Optional generation is disabled and operational enablement is blocked pending the
additive audit contract review in `docs/ingestion/context-audit-contract-proposal.md`.
The pure generation engine is covered offline, but durable dispatch/cache wiring
remains a proposal until that review. Its intended behavior: generation is disabled
by default and goes through the existing LLM gateway. It has explicit model, per-call output, input-character, per-document
call and reserved-token ceilings. Reserve the conservative UTF-8 input bound
(including prompt/envelope) plus output tokens before dispatch; errors/unknown
usage keep that reservation consumed. Cache successful generated context by
tenant, document, source, evidence/context, prompt version, model and budget
fingerprint. Cache hits consume no model calls. Failed/oversized/over-budget
responses are omitted; deterministic context remains. Proposed cache rows are
operational and cascade with document deletion, not historical evidence retention.

Store deterministic context, generated context and fingerprint separately from
evidence. Generated context is explicitly labelled in its field/metadata. Search
mapping updates are additive for existing indexes. Hybrid query/ranking and
embedding inputs remain unchanged in this issue; selecting enrichment for those
paths needs measured ranking evaluation. Re-index projects stored context.

Acceptance tests: deterministic title/type/structure/units and stable lineage;
exact source slice preserved; no model call when disabled or budget exhausted;
cache reuse and source/model/config invalidation; cross-tenant cache isolation;
oversized/malformed model response and provider failure; generated labelling;
index publication in separate fields; citation hydration never includes context;
migration upgrade/downgrade and fail-closed RLS DDL (offline).

Migration uses next free origin/main revision 0047 (parent 0046); sibling drafts
also reserve 0047. A human must reconcile ordering before merging the stack.

Merge gate: hold until measured against the baseline evaluation; a human merges.

Verification at implementation head d59b8d6: citation exclusion, generation gates,
separate search fields and projection regressions pass offline; full backend,
frontend, native/wheel, fidelity and isolated packaging CI all pass.
[Backend run](https://github.com/k-sandhu/lumen-copilot/actions/runs/37956668841)
and [native run](https://github.com/k-sandhu/lumen-copilot/actions/runs/37956668781).
Operational generation remains gated; this verification does not claim live model
or datastore integration.
