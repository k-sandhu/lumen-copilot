# 16. Context engine & cache-first prompting (the runtime performance track)

- **Status:** Accepted *(direction approved by the sponsor in-session 2026-07-16; this ADR records it)*
- **Date:** 2026-07-16
- **Builds on:** [ADR-0003](0003-application-stack.md) (LLM-agnostic; every model call via the `llm/` gateway), [ADR-0004](0004-architecture-boundaries-and-adapters.md) (one owning module per concern; domain types only), [ADR-0011](0011-assistant-and-agent-runtime.md) (one shared `chat_runtime` for ad-hoc chat, assistants, and headless runs), [spec 0004](../specs/0004-security-and-domain-invariants.md) (INV-2 permissioned retrieval, INV-3 citations, INV-6 audit)
- **Scope:** SPIKE — the only deliverable of [#405](https://github.com/k-sandhu/lumen-copilot/issues/405); no product code. It records the design for [epic #404](https://github.com/k-sandhu/lumen-copilot/issues/404) and is **guidance** for its features ([#409](https://github.com/k-sandhu/lumen-copilot/issues/409)–[#416](https://github.com/k-sandhu/lumen-copilot/issues/416)). [ADR-0017](0017-hierarchical-memory.md) (memory) and [ADR-0018](0018-sub-agent-orchestration.md) (sub-agents) build on the segments and executor decided here.

## Context

The answer runtime is safely **bounded** everywhere — tool-turn budget, `k` caps, snippet caps, per-tool timeouts — but it is *count-based and cache-blind*:

- **The prompt is assembled inline** (`chat_runtime._answer`): system prompt + the last `_HISTORY_TURNS = 20` user/assistant **text-only** turns + the question. There is no token accounting anywhere; an oversize prompt fails *reactively* as the provider's `ContextWindowExceeded` → 422. Prior turns' tool calls, results, and citations are dropped entirely, so "expand point 2 from that doc" forces a blind re-search.
- **Nothing is cached.** One answer runs up to 20 sequential completion turns, each re-sending the full prefix (tool specs + system + history + the growing tool transcript) at full price with zero cache directives. Provider state (verified 2026-07): OpenAI-style caching is automatic on stable 1024+-token prefixes (now up to a 90% discount on cached tokens, retention up to 24h on newer models, with a `prompt_cache_key` routing hint); Anthropic-style caching is explicit `cache_control` breakpoints (~1.25× write, ~0.1× read). LiteLLM — our one gateway dependency — passes both through and can auto-inject breakpoints (`cache_control_injection_points`).
- **Tool calls execute sequentially** (`for call in turn_tool_calls: await …`) even though the gateway already *parses* parallel tool-call fragments correctly. A three-search turn pays the sum of latencies instead of the max.
- **A transient provider fault mid-answer kills the whole answer** (typed 503 terminal); `finish_reason == "length"` is unhandled; there is no fallback model. (Routing/fallback was explicitly fenced out of #25 — this ADR closes that deferral at the *turn* level.)
- **Pre-tool narration is dropped** (the #148 fix): a tool-calling turn's text is neither streamed nor persisted, which keeps the persisted answer equal to the streamed answer — but it also means *nothing* streams until a whole turn completes, so long agentic answers look stalled.

These are one design problem, not five. Caching, compression, and (later) memory all constrain each other through **prefix stability**: a naive sliding history window invalidates the cache every turn; a mid-answer memory write invalidates everything after it. The consensus architecture across leading runtimes (Claude Code compaction / Anthropic context editing + server-side compaction, OpenAI truncation strategies, Manus's cache-first context engineering, LangGraph summarization nodes) is a **deliberate context assembler with a stability gradient** — which is what this ADR adopts.

## Decision

### 1. A segmented, token-budgeted context assembler owned by `llm/`

A new `backend/app/llm/context.py` (inside the existing `llm/` boundary — prompt assembly is part of "the model boundary", so **no new AGENTS.md §6 row is needed**) owns turning the run's inputs into the message list. The prompt becomes ordered **segments with a stability gradient** (most stable first):

| # | Segment | Content | Stability |
|---|---|---|---|
| 1 | `tools` | The advertised tool specs (already deterministically sorted) | Per allow-list |
| 2 | `system` | user custom instructions → assistant instructions → `GROUNDED_SYSTEM_PROMPT` | Per session |
| 3 | `memory` | **Reserved, empty in v1** — filled by ADR-0017; versioned, changes only between answers | Semi-stable |
| 4 | `summary` | **Reserved in v1** — the rolling session summary (§3.2); versioned, changes only between answers | Semi-stable |
| 5 | `history` | Verbatim recent turns, **token-budgeted** (replaces `_HISTORY_TURNS`) | Rolls at answer boundaries |
| 6 | `live` | The question + this answer's growing tool transcript | Append-only within the answer |

Mechanics:

- **Budgets:** the model's input budget comes from `litellm.get_model_info(model).max_input_tokens` with a conservative fallback when unknown; reserve output headroom; count with `litellm.token_counter`. Each segment gets a budget; the **degrade order** is: shrink `memory` → roll more `history` into `summary` → shrink retrieval `k`/snippet budgets → honest typed refusal. An oversize prompt is a *deliberate* decision, never a provider 422.
- **Determinism:** stable serialization (sorted tool specs — already true; stable JSON key order; no timestamps or per-request values in segments 1–4).
- The assembler is pure over its inputs (unit-testable offline); `chat_runtime` composes it.
- **Semantic note (recorded decision):** the existing instruction order (user → assistant → grounding) is *kept* in v1. It costs cross-user prefix sharing (a per-user segment sits early), but preserving the documented precedence semantics beats a cache micro-optimization; the intra-session and in-answer caching below is where the real money is. Revisit only with eval evidence.

### 2. Cache-first prompting rules (the invariants the assembler enforces)

1. **Stable prefix ordering** — the segment table above; volatile content only at the tail.
2. **Append-only within an answer** — the loop only ever appends messages (already true); never mutate earlier messages mid-answer *except* the chunked compaction in §3.1, which is threshold-triggered precisely to amortize invalidation.
3. **Trim only at answer boundaries** — history rolls into the summary between answers, never mid-loop, and in **chunks** (roll K turns at once), so the prefix changes once per roll rather than every turn.
4. **Breakpoints:** for Anthropic-style providers, `cache_control` marks at the segment 2/3/4 boundaries plus a **moving mark on the last message of each loop turn** (incremental caching), injected in the gateway via LiteLLM; for OpenAI-style providers, no marks — prefix stability does the work, and the gateway passes `prompt_cache_key = session_id` as the routing hint. Providers without caching support get no directives and identical behaviour to today.
5. **Mask, don't remove** — the advertised tool set stays stable for the life of a session (per-session allow-lists already are); MCP `discovered_tools` snapshots are persisted, so re-discovery (which may reorder/change specs) naturally lands between sessions.
6. **Measure it** — cached-read / cache-write tokens are recorded per answer via the usage substrate ([#409](https://github.com/k-sandhu/lumen-copilot/issues/409)): `TokenUsage` gains `cached_prompt_tokens` + `cache_write_tokens`, the `done` envelope reports them (additive contract change), and a per-answer `llm_usage` row (tenant/session/message/model + the five token fields) is the substrate for the cache-hit KPI, AgentOps ([#300](https://github.com/k-sandhu/lumen-copilot/issues/300)) and the ADR-0018 budgets.

### 3. Compression (three timescales)

**3.1 In-answer tool-result compaction** ([#415](https://github.com/k-sandhu/lumen-copilot/issues/415)). When the live transcript crosses a threshold (a fraction of the input budget), replace the **oldest** tool results' `content` with `[cleared — {summary}]`, keeping the calls and the most recent results verbatim. The one-line `summary` every `ToolResult` already persists is the placeholder text. Clearing happens in **chunks** (≥N results at once) to amortize the cache invalidation it necessarily causes — the same trade Anthropic's `clear_tool_uses` context editing makes. Citations are unaffected: `GroundedCitation`s are recorded at retrieval time (INV-3 structural), not re-read from the transcript.

**3.2 Rolling session summary + token-based window + evidence carry-forward** ([#416](https://github.com/k-sandhu/lumen-copilot/issues/416)).

- A `session_summaries` row per session (`session_id`, `summary`, `covers_through_message_id`, `version`), updated **asynchronously post-answer** (Celery, ADR-0015 infra) — the hot path never pays for summarization, and the prompt prefix only changes at answer boundaries (cache-aligned). Summaries are per-session and sessions are per-user, so INV-2 holds by construction; the summarizer runs over content the session's user already saw.
- Assembly consumes `[summary vN] + [last M turns verbatim]` with M token-budgeted — this **replaces** `_HISTORY_TURNS`.
- **Evidence carry-forward:** each answer persists a compact digest of what it cited (document ids + chunk ids + one line each — data already in hand at persist time); the *next* answer's `live` segment includes the previous digest, so follow-ups can target `get_document`/`search_text` by id instead of re-searching blind.
- Summarizer failure degrades to today's behaviour (verbatim window only) — never a failed answer.
- The eval harness gains a **compression-regression suite**: golden multi-turn conversations must hold groundedness/citation metrics with compacted context.

### 4. Turn-level resilience ([#413](https://github.com/k-sandhu/lumen-copilot/issues/413))

A loop turn is **buffered and pure** — `_stream_one_turn` publishes nothing, and tools run *between* turns — so retrying a failed turn is safe by construction (no duplicate user-visible text, no re-executed side effects). Therefore:

- Retry a turn ≤2 times with backoff on `DependencyError` (transient 5xx/timeout/connection).
- Then walk the tenant's **fallback model list** (an ordered list on the existing per-tenant provider routing; each route resolved once, keys never re-decrypted per attempt). The `done` envelope and the answer audit record the model that actually answered.
- `finish_reason == "length"`: one continuation turn stitched onto the buffered text before the terminal.
- Exhausted routes → exactly one typed terminal `error`, exactly as today; vendor errors still never leak.

### 5. Concurrent tool execution semantics ([#412](https://github.com/k-sandhu/lumen-copilot/issues/412))

- The calls of one turn run under `asyncio.gather` with a small semaphore. `tool_call` events are emitted at **dispatch** (with the audit `ordinal` assigned at dispatch); `tool_result` events emit as each completes; transcript messages append in **original call order** (provider protocol + cache determinism).
- **Sessions:** `AsyncSession` is not concurrency-safe, and both the retrieval tools (permission predicates) and the runner's `_finalise` (audit + `tool_invocations`) touch it. The `ToolContext` therefore carries a **session factory**; each concurrent handler opens its own short-lived session (and its own `RetrievalService`). `_finalise` stays **serialized** on the runtime's session. The governed per-call path (allow-list → autonomy → approval → bounded execute → uniform result → audit) is unchanged.
- v1 conservatism: read-only **T0** calls run concurrently; side-effecting (T1+) calls in the same batch execute serially after the T0 batch.

### 6. `event:thinking` — narration becomes visible, the answer invariant stays ([#414](https://github.com/k-sandhu/lumen-copilot/issues/414))

A tool-calling turn's text streams live as a new **`event:thinking`** envelope (additive `contracts/` change) instead of being silently dropped. It is **never** a `delta` and **never persisted**, so the #148 invariant — the streamed answer equals the stored message — holds untouched; clients render it as a transient status affordance. Replays do not re-materialize thinking text into the message.

## Consequences

- **Cost/latency:** 50–80% input-cost reduction on tool-heavy answers (in-answer prefix reuse dominates), 2–4× lower tool latency on multi-call turns, and answers that survive provider weather. The KPI is observable from day one via #409.
- **Complexity moves to one place:** prompt construction stops being incidental and becomes a tested component with declared budgets. That is new surface, but it is the surface ADR-0017 (memory segment) and ADR-0018 (worker budgets/executor) already need — built once.
- **Cache invalidation is now a managed trade:** summary rolls and compaction chunks deliberately pay occasional cache-write costs to keep steady-state hits high.
- **The eval harness becomes load-bearing** for this track: compression and caching changes land only behind the groundedness/citation/recall gates (the OD-7 CI lane wires them; until then they run locally per the Definition of Done).
- Deliberately **not** decided here: server-side provider compaction betas (we compress client-side and stay provider-agnostic), cross-user prefix sharing (semantic order kept), KV-cache-aware routing across multiple deployments (single-deployment today; a LiteLLM router concern for later).
