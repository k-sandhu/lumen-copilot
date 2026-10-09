# Constrained-decision capability (#690)

Design: [ADR-0028](../../../docs/architecture/0028-hierarchical-document-classification.md).
`DecisionsGateway.decide` accepts bounded text and vendor-free choice, predicate
and score questions; it returns typed answers or `DecisionError`. It does not
classify documents or schedule work. Reuse one gateway instance per tenant/worker to
enforce its concurrency semaphore. Provider-specific JSON stays in `llm/`.

Deployment settings use the `DECISIONS_*` environment aliases in `core/config.py`:
enablement (false), candidate model, optional fallback model, timeout (30s), retries
(1), backoff (0.5s), concurrency (2), input/response byte caps (32768/262144), question
and option caps (32/64), fallback output cap (4096), option order (`fixed`) and
shuffle seed (0). Cost budget and per-call ceiling default to zero and deny calls.
The byte limit covers evidence plus questions/options; #691/#692 must enforce a
separate tokenizer-based bound before calling it. No PDFs/files/images in this slice.

`decision_policy_from_settings` seeds controls without enabling a tenant.
The trusted calling service resolves tenant model allow-lists/credentials through
the existing provider-model resolution and secrets seams, validates tenant
enablement, and constructs `DecisionPolicy`. The process candidate/fallback and
cost settings can seed tenant controls, but never enable a tenant implicitly.
Fallback capability must be verified from the model's `supported_parameters`,
including `structured_outputs`, before setting `fallback_structured_outputs`.
Only OpenRouter chat routes are supported by this strict fallback. The provider
enforces parameters and LiteLLM retries are disabled so every dispatch is counted.

Supply a tenant-bound `CanonicalDecisionLedger` backed by a shared durable budget
store and the existing `AuditSink`. Its store reserves each attempt's ceiling
atomically against the selected tenant budget and persists actual model usage/cost
on settlement. The same attempt ID makes store operations idempotent. Unknown
spend holds the ceiling; known spend replaces it. Intent/terminal events use the
canonical durable audit path. Store/sink failure prevents a call or returned result.
The two additive event types are declared in the existing OpenAPI audit enum and
its frontend mirror; no classification-resource fields or new endpoints are added.
The store protocol is an integration seam: #692 must provide database-owned atomic
storage, a budget-window policy, stage context and wiring; this capability ships
no process-local production budget and is not enabled by app startup.

Choice order is either the supplied order or a reproducible seed/question-name
shuffle, recorded on every attempt. Score levels never shuffle. Changing order,
seed, taxonomy or model invalidates a calibration assumption. Fallback estimates
are marked `structured_output`; no estimates are represented as calibrated.

HTTP 429/5xx/transport/timeouts retry within configured limits; 404/405/410 allow
configured fallback without retry. Authentication/other rejection, malformed
responses and refusals fail closed. Missing or invalid cost/tokens also fail
closed with unknown spend retained. A successful answer whose reported cost
exceeds its reserved ceiling is recorded and rejected; the ceiling must be a
conservative bound for the configured provider, not a guarantee imposed on its
billing API. Cancellation leaves the committed intent and held reservation for
reconciliation. `total_cost_usd=None` means at least one attempt has unknown cost.

Audit metadata excludes evidence, provider errors and credentials; it includes
request/resource attribution, fingerprint, requested/reported model, method,
order/seed, attempt identity, typed error and usage/cost. Required audit failures
never become successful results. Tenant/ACL/authentication/citation/override and
search-readiness integration remains #692/#693, which must add matching spec 0004
negatives. This slice touches INV-1, INV-6 and INV-8 through tenant binding,
mandatory audit and input/response validation.

Offline recordings and synthetic tests are under `tests/fixtures/decisions`.
No live model smoke was used and no money was spent. Alpha shape and the
configured candidate model remain unverified by inference. Baseline evaluation,
calibration, provider privacy approval, budgets and retention remain owner gates.

Merge gate: hold until measured against the baseline evaluation; a human merges.

## Verification record — 2026-10-08

- 46 capability tests passed, including negative cases, the real gateway's
  strict-schema arguments with a stubbed LiteLLM module, and shuffled schema order.
- Scoped gateway/tool/audit/capability regression: 222 passed, 4 live tests excluded.
- Changed-file Ruff checks/format and strict mypy on the four capability files passed.
- API regeneration, the frontend audit-taxonomy test (1 test) and TypeScript check passed.
- [~] 2026-10-08: full backend suite skipped because the final RAM check was
  2785 MiB, below the required 4000 MiB; residual risk: unrelated regressions remain untested.
- [~] 2026-10-08: explicitly checking `core/config.py` reports eight existing
  missing environment-alias arguments at the unchanged `get_settings()` constructor;
  residual risk: repository-wide strict typing remains incomplete.
- [~] 2026-10-08: live inference/datastore checks not run under this task's limits;
  residual risk: alpha endpoint behavior and durable stage integration need #692/#694
  verification. No containers were started/stopped; inference spend was zero.
