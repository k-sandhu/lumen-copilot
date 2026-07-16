# 18. Sub-agent orchestration (orchestrator–worker fan-out)

- **Status:** Accepted *(direction approved by the sponsor in-session 2026-07-16; amended same day for the [#417 review](https://github.com/k-sandhu/lumen-copilot/pull/417) findings before first merge — immutable thereafter; changes supersede. Build lands with the [#299](https://github.com/k-sandhu/lumen-copilot/issues/299) epic when it un-gates)*
- **Date:** 2026-07-16
- **Builds on:** [ADR-0016](0016-context-engine-and-cache-first-prompting.md) (the §5 concurrent executor + per-call isolated scope, §3 compression, §2.6 usage ledger), [ADR-0011](0011-assistant-and-agent-runtime.md) (one shared runtime; `AssistantRunConfig`; the assistant's narrowing `collection_ids`), [ADR-0014](0014-web-search-provider.md) (web evidence is a distinct citation type), [ADR-0015](0015-scheduling-and-headless-runs.md) (`RunTranscriptSink`), [spec 0004](../specs/0004-security-and-domain-invariants.md) (INV-1/2/3/6/7)
- **Scope:** SPIKE — the only deliverable of [#407](https://github.com/k-sandhu/lumen-copilot/issues/407); no product code. Guidance for the features cut under #299.

## Context

The consensus for hard, decomposable questions is **orchestrator + isolated parallel workers with summary-only returns** (Anthropic's research system beat single-agent Opus by 90.2% on their eval at ~15× the tokens — *external reference*; the lesson that transfers is context isolation + budgets-as-architecture, not the number). Lumen's runtime is engine-not-policy (ADR-0011) and every tool call is governed (ADR-0007-era runner), so orchestration should compose the existing machinery — **but the review surfaced five places where "just reuse it" is wrong**: worker permissions are not just the principal, worker passages are not `GroundedCitation`-ready, there is no persistence schema for a worker run, there is no injected executor seam (a static tool handler gets only a `ToolContext`), and live budget counters cannot prevent overshoot. This ADR decides those.

## Decision

### 1. Surface + the injected executor seam

A `dispatch_subagents` tool (auto-discovered `impls/` module; `default_offered=False`; assistant-gated; **T0/read-only** since workers get only read-only tools). But a static tool handler receives only a `ToolContext` — it has no `ChatRuntime`, gateway, session factory, usage ledger, or executor. So the runtime injects a **`SubagentExecutor` protocol** (the ADR-0011 one-runtime rule made structural): the handler calls `ctx.subagents.dispatch(requests)`; the executor is built by the shared runtime from the **existing loop machinery factored behind the seam** (no second loop). Typed `WorkerRequest` (objective, expected-output shape) and `WorkerPolicy` (budgets, model, scope) cross the seam; `ctx.subagents is None` (a run that didn't wire it) ⇒ a typed `ok=False`, exactly like the sandbox seam.

### 2. Worker scope — an immutable `WorkerScope`, not "same principal"

"Same `Principal`" is insufficient: an assistant's `collection_ids` **narrow** what the principal may retrieve (ADR-0011 / `assistant_runtime`), and passing only principal+allow-list would let a worker read a document the *user* owns but the *assistant* was not scoped to. Workers therefore inherit an immutable **`WorkerScope`** carrying: principal, tenant, assistant/version id, the effective **`collection_ids`** (the parent's already-narrowed set), knowledge-mode/source restrictions, and the effective tool allow-list (**parent ∩ read-only T0**; `web_search` only if the parent had it). Retrieval applies *all* inherited restrictions; the required test retrieves a document permitted to the user but outside the parent assistant scope and asserts it is refused. Depth cap 1 is structural: `dispatch_subagents` is never in a worker's allow-list.

### 3. Worker results & citations — typed evidence, rehydrated and re-checked

A worker returns a **structured summary plus a typed evidence list** — never a `GroundedCitation` directly, and the parent **never cites the worker's generated summary text**. The current runtime builds citations only from `ToolResult.passages` (`RetrievedPassage`), and `web_search` returns provider payload, not passages — so the ADR defines a **typed worker evidence union**: corpus evidence = exact `RetrievedPassage` domain values (document + chunk ids + spans); web evidence = the distinct ADR-0014 web-evidence type (URL + fetched snippet). The parent **rehydrates each evidence item through `retrieval/` under the current principal+scope and re-checks permission** before it becomes a citation (a document revoked between worker retrieval and parent answer is dropped — the required test). Corpus evidence flows into `GroundedCitation`; web evidence into the web-citation path (INV-3 honored per source type).

### 4. Budgets — atomic reservation, not live counters

Live aggregate counters cannot prevent overshoot (N workers start large calls before any reports usage; provider usage arrives only post-consumption). So the executor **reserves an atomic token slice per worker before dispatch** from the answer's aggregate budget, sets the provider `max_tokens` on worker calls to the reservation, counts failed attempts against it, and **returns the unused remainder on completion**; a worker whose reservation cannot be met is declined (its task returns `ok=False` "budget exhausted", completed work kept). The aggregate budget is a per-answer cap composed with the tenant cap (`min`); whether the lead's own tokens share the cap is **decided: they do** (one answer, one budget). Workers may run a cheaper model via a **new `worker_model` field on the versioned `AssistantRunConfig`**, route-authorized and fallback-governed exactly like the primary (ADR-0016 §4). Every worker's spend is a **row in the #409 ledger** (`model` = actual, `message_id` NULL, keyed by the worker/run id).

### 5. Persistence & audit — a decided schema, incremental, governed

`RunTranscriptSink` persists to an existing `Run` that requires owner+assistant and has no parent-message/worker relationship, and a deferred FK only helps within one transaction — independent worker sessions would fail at commit before the parent message exists. So this ADR decides a concrete **`subagent_runs`** schema: `tenant_id`, `owner_id`, parent `session_id` + parent `message_id`, `worker_id`, objective hash, effective-scope snapshot, model, status lifecycle, token totals. The **parent identity (a stable execution/answer id) is created before any worker commits**, so worker rows never dangle; workers persist **bounded steps incrementally** (not only at completion) so a crash stays auditable. Orchestration lifecycle audits are first-class: `subagent.started/progress/completed/failed/cancelled` with parent+worker ids, objective hash, effective scope, model, token totals, outcome. **Public progress envelopes carry status/counters only** — never worker transcript text (the summary-only rule enforced at the transport, not just by convention); stored transcripts have defined access control.

### 6. Failure containment

A worker that fails/times-out/dies yields an `ok=False` task summary; the parent continues with whatever completed (issue #207 §7 contract). The dispatch call is wall-clock bounded so a hung worker cannot stall the answer past its budget; a cancelled worker returns its unused reservation (§4).

## Consequences

- **#299 gets a real engine**, but the net-new work is explicit and non-trivial: the `SubagentExecutor` seam, `WorkerScope` inheritance, the typed-evidence rehydration path, atomic budget reservation, and the `subagent_runs` schema — none are "just reuse `chat_runtime`."
- Cost is real and now *bounded and attributed* (§4 reservation + #409 ledger rows + tenant cap), and *visible* (§5 audits, counter-only progress).
- v1 fences, each a later decision: workers with write/approval-gated tools, depth > 1, cross-principal delegation (never — INV-2), long-lived/background workers (ADR-0015's lane), and lead/worker sharing a model cap across *different* tenants (never).
