# ADR-0028 — Hierarchical document classification

Status: **Proposed — implementation drafts authorized for evaluation; human adoption required.**
Date: 2026-10-08. Tracking: [#688](https://github.com/k-sandhu/lumen-copilot/issues/688), epic #661.
Builds on ADR-0004, ADR-0006, ADR-0025 and spec 0004. ADR-0026 records continuous integration; ADR-0027 is reserved for the Rust-core
proposal. ADR-0028 was free on origin/main when checked on 2026-10-09.

## Context

File formats do not identify business document types. Search, agents and
processing profiles need a shared, detailed taxonomy without introducing an
access-control authority or replacing original evidence. The provider's alpha
API and uncalibrated probabilities require a replaceable gateway and measured
release gates. This ADR specifies #689 and #690; #691–#694 own integration and
evaluation. No classification runs automatically through these first two capabilities.

## Decision

### Taxonomy and compatibility

Keep immutable JSON releases and a JSON Schema in
`backend/app/classification/data/`; a loader/validator owns those files. They
are backend reference data, not a new API wire contract. #693 must freeze API
fields in contracts before building UI/search surfaces (ADR-0006).

Each release has a semantic version and nodes with stable slash-delimited IDs,
label, precise description, examples and positive signals. Hierarchy is domain
→ family → type → optional subtype, with `other` at every sibling decision,
including the root and optional subtype decisions. A selected `other` remains
a real taxonomy path, distinct from a failed/unclassified result. Every domain
and family has descendants through the type level. Cover legal,
financial/accounting, tax, regulatory/permitting, environmental, health/safety,
technical/engineering, scientific/academic, medical, government/legislative,
HR/policy, sales/marketing, correspondence, forms, data exports, presentations.
Independent facets include scanned, contains tables, personal data, draft,
signed and multi-document bundle; unknown evidence must remain unknown rather
than false. Facets describe evidence and never confer permissions or certify compliance.

Published files are append-only. IDs are never reassigned to different meanings.
An ID removal/rename needs an explicit version-to-version mapping to an existing
replacement or null (retired/unclassified); removed IDs remain tombstones and
cannot be reused. Semantic changes require a new ID and mapping, even when the
label is similar. Label/description clarifications retain IDs and are reviewed.
Validation compares every historical release, rejects incomplete mappings and
ID reuse, and checks parentage/depth, sibling `other`, examples and signals.
Classification retains its original taxonomy version; migration never silently
rewrites history. Tenant-custom taxonomy authoring is outside #689.

### Placement, inputs and cascade

After canonical extraction, queue classification alongside chunking/indexing.
Its dependency is extracted evidence, never search publication. A failed,
disabled, pending or review-required classification **never delays search
readiness and never changes visibility**. ACL freshness, retrieval filters and
current tenant permissions remain authoritative. Classification/profile changes
must not mutate original text, source offsets or historical citation generations.

#691 computes features and bounded representative excerpts in Rust: title,
headings, table headers, first/representative regions, signature blocks, counts,
format and structural signals, with exact extraction-generation provenance.
No whole document, external URL, PDF/file payload or cross-document tenant
context goes to the classifier. A configurable tokenizer-specific input budget
counts evidence plus instructions/options; a gateway byte cap is additional,
never presented as a token count. Image inputs require separately approved
sampling/egress limits; #690 initially accepts bounded text only.

Unambiguous, versioned rules may classify first; conflicting rules defer to the
model and never override a user choice. Otherwise ask a root choice question,
then only children of the selected node until a type/optional subtype. Persist
each conditional distribution: a child probability is conditional on the
parent, not a marginal probability. Ask facets as independent yes/no questions
in one request. Preserve completed levels if a later level fails, but mark the
overall classification incomplete; never manufacture descendants. Multi-document
bundles receive the bundle facet and a conservative path; splitting is separate work.

### Model gateway and failure policy

All calls remain inside `backend/app/llm/`. Use `httpx.AsyncClient` for
OpenRouter `POST https://openrouter.ai/api/alpha/decisions`, with configured
model (initial candidate `openai/gpt-6-luna-decisions`). Its `state`, keyed
`questions`, `criteria`, `noul` and keyed `answers` are private wire shapes.
Expose vendor-free choice, predicate and score domain objects with usage/cost.
Scores are probability-weighted ordinal indices, not categorical choices.

If retries exhaust on a transient fault or the endpoint is unavailable, an
explicitly configured chat fallback goes through the existing LiteLLM gateway
with strict `json_schema` and `provider.require_parameters=true`. Only a model
whose verified capabilities include `structured_outputs` is admitted; lack of
support is an error, never a prompt-only JSON fallback. Auth/configuration faults,
refusals and malformed responses fail closed without silent fallback. Validate
all answer names, allowed values, complete normalized distributions, finite
probabilities/confidence/score, usage counts and cost before returning an answer.
Fallback probabilities are model-reported estimates, distinguished by method.

Fixed caller option order is the reproducible default; seeded shuffle is an
explicit alternative. Record actual order for each attempt. Never shuffle score
levels. Evaluation varies choice order and measures sensitivity. No reported
probability or confidence is treated as calibrated by default.

Disabled/unconfigured provider, exhausted budget, timeout, refusal and invalid
response produce typed `unclassified` reasons, never guessed labels. #692 owns
persisted status, bounded backoff/retry scheduling and idempotency keyed by tenant,
document, extraction/input fingerprint and taxonomy version. Retry only transient
conditions; reconfiguration/new evidence can explicitly retry permanent failures.

### Tenant controls, accounting and data handling

Classification starts disabled until an administrator enables it with an allowed
model, bounded excerpt/token/byte limits, per-request timeout, retry count,
concurrency, per-call ceiling and per-tenant cost budget. Missing controls deny.
Use the existing tenant model/provider resolution seam for credentials; credentials
stay ephemeral and absent from outputs/audit. No chat-picker selection implicitly
enables classification. Disabling prevents new dispatches; existing results remain
readable under their document permissions. Controls/UI persistence belong to #692/#693.

Reserve a conservative per-attempt cost ceiling atomically against the tenant's
budget before dispatch, including every retry and fallback. Record actual provider
usage and cost per attempt. Missing accounting or outcome-ambiguous requests retain
the reservation as unknown spend; do not free money that may have been spent.
Cross-process budgets must use a shared tenant-scoped ledger, not process-local
counters. #690 provides a mandatory injected ledger contract; #692 wires the
durable stage ledger. Denied admission and failed audit prevent dispatch/results.

Mandatory intent and terminal events go through the existing canonical audit sink:
`model.decision_requested` and `model.decision_completed`, carrying trusted tenant,
system/user actor, request/resource identity, method/model, input fingerprint,
actual option order, attempt, usage/cost or unknown accounting, and safe typed
failure reason. Audit contains no excerpts, answers' free text, credentials,
personal-data values or raw provider errors. #692 adds classification/override/
reclassification lifecycle events and tenant-bound transactions. A missing audit
event fails INV-6. Cancellation leaves an intent and conservatively retained spend.

Only authorized document evidence from one tenant/generation enters each request.
Treat source text as untrusted data, never executable instructions. External
processing requires the tenant's approved provider/data-handling policy; personal
data or sensitive domains do not imply consent. Do not claim upstream retention,
training exclusion or residency from an API schema: owner approval must verify
OpenRouter and upstream policy before enabling real tenant documents. Public
fixtures are small synthetic examples or sanitized documentation responses;
larger authorized corpora stay private. No secret/content logging or fixture capture.

### Persistence, review and downstream use

#692 persists tenant/document and extraction IDs, source/input fingerprint,
status/reason/retry metadata, chosen path, per-level conditional probabilities
and confidence, facets with probabilities/unknowns, taxonomy version, method
(`rules`, `decisions`, `structured_output`, `override`), rule/prompt version,
requested and reported model, per-attempt and total cost (unknown distinguished
from zero), option orders, timestamps and review state. An override has actor,
reason and audit lineage and survives re-ingestion/reclassification. No new DB
schema is introduced by this ADR or #689/#690.

Before calibration, all model classifications require review and are advisory.
#694 measures held-out per-level accuracy, facet F1, calibration error, order
sensitivity, latency and total cost across rules/decisions/fallback. The owner
fixes tolerances, corpus and metric definitions before scoring; thresholds are
then derived from measurements per model/method/taxonomy version and recorded
with the calibration artifact. No arbitrary confidence cutoff is shipped.

#693 mirrors classification to the existing search store and adds taxonomy-prefix
and facet filters only behind permission filtering; no aggregate/badge leaks a
forbidden document. Agents use the same permissioned filters. Configuration maps
paths to processing profiles with a conservative default; classification cannot
hold initial indexing hostage. A later profile application is an explicit new
generation subject to the baseline fidelity/citation gates, never an in-place rewrite.

## Acceptance and consequences

Offline gates: schema/hierarchy/compatibility negatives (#689); recorded HTTP
choice/predicate/score and strict fallback tests; timeout, malformed, unconfigured,
budget/audit failure and option-order tests (#690). Integration negatives for
foreign-tenant inputs, permissions, overrides and independent readiness belong
to #692/#693. No live datastore tests are required for these capability drafts.

The cascade bounds option sets but can propagate a wrong parent; distributions,
review and held-out per-level evaluation expose that risk. Alpha endpoint churn,
unknown spend and uncalibrated fallback estimates remain operational risks.
Owner decisions before release: approved data handling/model allow-list; measured
thresholds/tolerances and corpus; deployment budgets; generation retention and
profile promotion policy. Draft code does not resolve these by implication.

**Merge gate: hold until measured against the baseline evaluation; a human merges.**

## Provider references (re-checked 2026-10-08)

- [OpenAI Decisions guide](https://developers.openai.com/api/docs/guides/decisions):
  typed text/image questions; dependent questions require separate requests.
- [OpenRouter decisions reference](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request):
  alpha endpoint and its distinct keyed question/answer format.
- [OpenRouter structured outputs](https://openrouter.ai/docs/features/structured-outputs):
  strict schema and parameter enforcement; verify model support.
- The supplied OpenRouter skills URL could not be retrieved; the official API
  reference above was available. No live inference call was needed for this ADR.
