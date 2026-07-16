# 18. Sub-agent orchestration (orchestrator–worker fan-out)

- **Status:** Accepted *(direction approved by the sponsor in-session 2026-07-16; this ADR records it — build lands with the [#299](https://github.com/k-sandhu/lumen-copilot/issues/299) Research & Artifacts epic when that epic un-gates)*
- **Date:** 2026-07-16
- **Builds on:** [ADR-0016](0016-context-engine-and-cache-first-prompting.md) (the concurrent executor §5, compression §3, usage substrate §2.6 — fan-out is unaffordable without them), [ADR-0011](0011-assistant-and-agent-runtime.md) (the one shared runtime; assistant allow-lists), [ADR-0015](0015-scheduling-and-headless-runs.md) (`RunTranscriptSink` — persisted transcripts without a socket), [spec 0004](../specs/0004-security-and-domain-invariants.md) (INV-1/2/3/6/7)
- **Scope:** SPIKE — the only deliverable of [#407](https://github.com/k-sandhu/lumen-copilot/issues/407); no product code. Guidance for the features that will be cut under #299.

## Context

The 2026 consensus for hard, decomposable questions is **orchestrator + isolated parallel workers with summary-only returns**. The reference result (Anthropic's research system): a lead agent decomposes the question and spawns parallel subagents — each with its own context window, narrowed tools, and an explicit objective — and the multi-agent system beat single-agent Opus by 90.2% on their research eval **at ~15× the tokens of a normal chat**. Two lessons transfer directly: *context isolation* (worker transcripts never pollute the lead) is what makes it work, and *budgets are part of the architecture*, not an afterthought.

Lumen's shape is unusually ready for this: the runtime is already engine-not-policy (ADR-0011 — the same loop serves ad-hoc, assistants, and headless runs), every tool call already flows through one governed runner (allow-list → autonomy → approval → audit), permissions key off the `Principal` inside `retrieval/` (INV-2), and ADR-0015 already persists socket-less transcripts. Orchestration composes these; it invents almost nothing.

## Decision

### 1. The surface: a `dispatch_subagents` tool

One new registry tool (auto-discovered `impls/` module, like every other):

```
dispatch_subagents:
  tasks: [{ objective: str, expected_output: str }]   # 1..N, N capped by config
  max_parallel?: int                                   # clamped by the server
```

Governance: `default_offered=False` (assistant-gated only — never ad-hoc), **T0/read-only in v1** because workers get only read-only tools (§2); the runner still allow-lists, bounds, audits, and traces the dispatch call itself like any tool.

### 2. Workers: bounded mini-loops on the existing machinery

Each task becomes one worker: a bounded agent loop **reusing the chat-runtime machinery** (not a fork), with:

- **Its own transcript** (system prompt = a worker contract + the objective + the expected output shape). Worker transcripts **never** enter the parent context — the parent sees only §4's summaries. This is the load-bearing isolation property.
- **The same `Principal`** as the parent run. Retrieval's permission filter keys off the principal, so a worker can never read what the user couldn't — INV-2 holds *by construction*, exactly as ADR-0015 argued for headless runs.
- **Allow-list = parent ∩ read-only T0** in v1 (retrieval tools; `web_search` only if the parent had it). No write tools, no approval-gated tools in workers — a worker can gather, not act.
- **Depth cap 1, structurally:** `dispatch_subagents` is never in a worker's allow-list, so recursive spawn is impossible rather than discouraged.
- **Its own `ToolRunner`** → per-worker `tool_invocations` rows and `tool.*` audit events (INV-6), tagged with the parent session/message and a worker id.
- **Its own budgets:** a small tool-turn budget (~5) and a per-worker token slice of the answer's aggregate budget (§3).

### 3. Budgets (the 15× lesson)

The dispatch handler enforces a **per-answer aggregate token budget** across all workers, read against the live counters of the usage substrate ([#409](https://github.com/k-sandhu/lumen-copilot/issues/409)); a tenant-level cap (config, admin-overridable later via the [#300](https://github.com/k-sandhu/lumen-copilot/issues/300) AgentOps surface) bounds the blast radius. Workers may run a **cheaper model** (an assistant-config override; the lead stays on the session model) — the lead-strong/workers-fast pattern. Budget exhaustion is graceful: remaining tasks return `ok=False` "budget exhausted" summaries; completed work is kept.

### 4. Results: summary-only returns; citations flow unchanged

Each worker returns a **structured summary** (findings + the passages it retrieved, with their ids) — never its transcript. The dispatch tool folds these into one `ToolResult` for the parent loop. Because worker passages arrive through the same `retrieval/` path under the same principal, they feed `GroundedCitation` exactly as the parent's own retrievals do — **INV-3 needs no new machinery**: the parent's answer cites worker-retrieved passages as first-class permitted evidence.

### 5. Transport & persistence

- Live progress rides the parent stream with its monotonic `seq`: `event:subagent_started` / `event:subagent_progress` / `event:subagent_completed` (additive `contracts/` envelopes, mirroring the ADR-0013 `code_output` pattern of interleaving child activity into the parent stream).
- Worker transcripts persist through the **ADR-0015 `RunTranscriptSink` shape**, linked to the parent message — inspectable after the fact (the run-trace UI of #300 reads them), not re-streamed.

### 6. Failure containment

A worker that fails, times out, or dies mid-loop yields an `ok=False` task summary; the parent loop continues with whatever completed (the same "failure is a result, never a crash" contract as every tool, issue #207 §7). The dispatch call itself is bounded by a wall-clock ceiling so a hung worker cannot stall the answer past its budget.

## Consequences

- **#299 (Research & Artifacts) gets its engine:** deep research = `dispatch_subagents` + the compression/citation machinery + `write_file` for the report artifact — a governed platform capability rather than a bespoke feature.
- Cost is the real price: fan-out answers can spend an order of magnitude more tokens; the §3 budgets + #409 metering + tenant caps make that a *visible, bounded* spend. Fan-out without ADR-0016's caching/compression would be reckless; sequencing is deliberate.
- v1 fences that would each be a **new decision** if widened: workers with write/approval-gated tools, depth > 1, cross-principal delegation (never — INV-2), and long-lived/background workers (that is ADR-0015's lane, not this one).
