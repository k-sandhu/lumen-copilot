# Prompt-cache scope spike (#760), 2026-10-15

> **What this is.** The measurement record behind the ADR-0016 §2 and §3.1 amendments of [#760](https://github.com/k-sandhu/lumen-copilot/issues/760). It records three cache losses found by notebook 06 (2026-10-14), the provider facts that explain them, a live A/B/C test of the fixes, and the recommendation for each loss. No product code changed. The prototype ran as eval-only patches that were never committed.

## 1. The three losses (baseline, notebook 06, origin/main ce4d853)

| # | Loss | Measured |
|---|---|---|
| 1 | `prompt_cache_key = session_id` (`chat_runtime.py` → `gateway.py` `extra_body`) | `openai/gpt-6-luna` read **0%** on call 1 of a new session, even for an immediate repeat of an identical question. Anthropic read 100%, DeepSeek/GLM 88–96%. |
| 2 | No routing hint for `deepseek/*`, `z-ai/*` (`_cache_family` → `None`) | Median prefix reuse 84% / 82%. Calls 2+ with **zero** cache: 14% / 36%. Hit rate 59% / 41% against ceilings of ~81–83%. |
| 3 | Proactive compaction rewrites the prefix (ADR-0016 §3.1, #491) | 31% of calls followed `context.tool_results_proactively_compacted`. Hit rate on them fell ~25 points (gpt-6-luna 83% → 59%). |

## 2. Method

**Stack.** The isolated `lumen-eval` compose project used by notebooks 05–07: origin/main ce4d853, backend `:47381`, the 95-PDF `answer-accuracy-eval` collection, and four chat models via OpenRouter (`anthropic/claude-haiku-5.5`, `deepseek/deepseek-v4.1-flash`, `openai/gpt-6-luna`, `z-ai/glm-5.3-flash`). The rolling summary, web search and sandbox were off.

**Per-call method.** Each arm ran temporary, never-committed patches in the eval checkout:
- One `eval.llm_call_raw` log line per agent-loop completion, with prompt, cached, cache-write and completion tokens taken straight from the provider's `usage` (OpenRouter's `prompt_tokens_details.cache_write_tokens`, which Lumen's ledger misses, #759), plus the OpenRouter generation id.
- `saved_tokens` / `before_tokens` added to the existing `context.tool_results_proactively_compacted` line.
- An arm switch, read per call so arms could flip without a restart.

Each generation id was then resolved through OpenRouter's `GET /api/v1/generation`, giving the **serving provider** and the **billed cost** of every call. Costs below are what OpenRouter charged, not estimates from a price table.

**Arms** (same harness, same questions, run back to back on 2026-10-15):

| Arm | `prompt_cache_key` (openai/*) | OpenRouter `session_id` | Proactive compaction |
|---|---|---|---|
| A | chat session id (main) | none (main) | on (main) |
| B | tenant scope `lumen-<sha256(tenant)[:24]>` | same tenant scope, every OpenRouter route | on |
| C | as B | as B | **off** |

An arm D with a per-conversation `session_id` was prepared but not run (§5.2).

**Workload per arm per model** (notebook 06's questions): E1 is 10 single-question sessions run sequentially. E3 asks E1's first question in a new session, then immediately repeats it in another new session. E2 is 4 three-message conversations. That is 24 answers per model per arm, with models run in parallel.

**Metrics.** Hit rate is token-weighted Σcached / Σprompt over agent-loop calls. *Call 1 (x-session)* excludes each arm's first E1 session, which only warms the prefix. *Ceiling 2+* = Σ previous-call prompt / Σ prompt, the most any prefix cache can read. *Switch 2+* is the share of calls 2+ served by a different provider than the previous call of the same answer.

## 3. Provider facts that explain the losses

**OpenAI (GPT-5.6+, which includes gpt-6-luna).** OpenAI routes caches itself on these models. `prompt_cache_key` is "separate cache accounting" per customer, which OpenAI notes "helps prevent cache-hit probing across customers". Cache writes are billed at **1.25×** input and reads at 0.1× ([OpenAI prompt-caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)). The earlier ~15 requests/minute-per-key overflow guidance is stated for pre-5.6 routing.

A direct probe on `openai/gpt-6-luna`, pinned to the OpenAI provider, used a ~2.7k-token system prompt, two different questions and fresh nonces:

| Keys on request 1 → request 2 | Request 2 (new question): cached / prompt |
|---|---|
| none → none | 2,668 / 2,685 |
| K → K | 2,667 / 2,684 |
| K → K, explicit `prompt_cache_breakpoint` on the system block | 2,668 / 2,685 |
| K1 → K2, explicit breakpoint | **0** / 2,690 |

**The key partitions the cache.** A per-session key gives every chat a private, cold cache, while a shared key or no key shares the prefix. No explicit breakpoint is needed.

**OpenRouter sticky routing** ([OpenRouter prompt-caching docs](https://openrouter.ai/docs/features/prompt-caching)):
- Tracked per account, model and conversation.
- The sticky key is the top-level `session_id`. Failing that, it is `prompt_cache_key`. Failing that, it is a hash of the first system message and the first non-system message, and then it activates only *after* a cache hit is observed.
- With `session_id`, stickiness starts at the first successful request. It expires after 10 minutes idle, and a provider error re-routes it.
- A manual `provider.order` turns sticky routing off.
- `session_id` is also Z.AI's session-affinity key.
- DeepSeek and Z.AI cache automatically. OpenRouter's caching doc gives DeepSeek a read of ~0.1× and Z.AI ~0.2×. DeepSeek's *own* endpoint for v4.1-flash lists $0.006 against $0.30 per M, i.e. **0.02×**. §4 prices at the listed first-party endpoint and reports the 0.1× sensitivity alongside it.

**Provider dispersion.** OpenRouter's endpoint lists on 2026-10-14 showed `deepseek/deepseek-v4.1-flash` with **33** endpoints (prompt $0.014–$0.60 per M tokens) and `z-ai/glm-5.3-flash` with **32**. `openai/gpt-6-luna` had 7 endpoints across OpenAI, Azure and Bedrock; `anthropic/claude-haiku-5.5` had 10 across Anthropic, Vertex, Bedrock, Azure and AWS. Each provider holds its own cache.

**Why compaction shrinks so little.** Two facts combine:
- `chat_runtime` records **every** retrieved passage as a citation at retrieval time (`cited[passage.chunk_id] = …`).
- Since #611, `rendered_snippet` is the *complete* passage.

The #491/BE-2 digest re-embeds every cited snippet verbatim. On a search result, then, it drops little more than the passage labels, while still rewriting a block the provider had already cached.

**Re-checked on origin/main 3c022fc**, after #757 "persist only inline-cited permitted evidence". The answer loop still calls `cited.setdefault(passage.chunk_id, …)` for every retrieved passage, and now also passes every web block as cited to the compactor. Only the *persisted* citations are filtered to inline-cited ones afterwards, so the compaction input this finding rests on is unchanged.

## 4. Results (24 answers per model per arm, 1,402 agent-loop calls)

| Metric | Haiku 5.5<br>A / B / C | DeepSeek V4.1 Flash<br>A / B / C | GLM-5.3 Flash<br>A / B / C | GPT-6 Luna<br>A / B / C |
|---|---|---|---|---|
| Agent-loop calls | 111 / 114 / 102 | 139 / 142 / 140 | 98 / 110 / 104 | 120 / 121 / 101 |
| **Hit rate, all calls** | 65% / 67% / **76%** | 47% / 62% / **70%** | 41% / 43% / 43% | 64% / 65% / **77%** |
| **Call 1 of a new session** (different question) | 96% / 96% / 96% | 55% / 67% / 87% | 12% / 60% / 69% | **0% / 98% / 98%** |
| Call 1, identical question in a new session (one pair per arm, so noisy) | 100% / 100% / 100% | 96% / 74% / 100% | 88% / 0% / 96% | **0% / 100% / 100%** |
| Hit rate, calls 2+ | 65% / 67% / 76% | 48% / 62% / 69% | 42% / 43% / 42% | 65% / 64% / 77% |
| Calls 2+ with **zero** cache | 0% / 0% / 0% | **12% / 3% / 1%** | **28% / 13% / 11%** | 0% / 0% / 0% |
| Calls 2+ that switched provider | 0 / 0 / 0 | 3% / 3% / 1% | 8% / 2% / 0% | 0 / 0 / 0 |
| Calls 2+ right after a proactive compaction | 31% / 26% / 0% | 51% / 47% / 0% | 19% / 24% / 0% | 36% / 43% / 0% |
| Cache-write tokens / answer (billed 1.25×) | 36k / 34k / 20k | – | – | 25k / 27k / 9k |
| **Effective input-price multiplier**¹ | 0.50 / 0.48 / **0.38** | 0.54 / 0.39 / **0.32** | 0.67 / 0.65 / 0.66 | 0.51 / 0.51 / **0.36** |
| Billed $ / answer, mean² (m = thousandths of a dollar) | 6.27m / 5.91m / 4.09m | 13.08m / 2.79m / 7.81m | 4.23m / 5.96m / 5.54m | 3.92m / 4.20m / 1.70m |

¹ Σ(uncached × 1 + cached × read ratio + written × write ratio) / Σ prompt tokens, priced at the **first-party** provider's ratios (Anthropic/OpenAI read 0.1×, write 1.25×; DeepSeek read 0.02×; Z.AI read 0.2×). 1.00 means "every prompt token at full price". This isolates caching from each provider's *price*, and from trajectory length. It does not isolate it from each provider's cache *quality*; see the sequential-arm caveat below. Priced at OpenRouter's documented ~0.1× DeepSeek read ratio instead, DeepSeek's row is 0.57 / 0.44 / 0.37: the same direction, with deltas within 0.01–0.02.
² What OpenRouter billed. The DeepSeek and GLM figures are dominated by the serving provider's price. Prompt prices for one model span $0.014–$0.60 per M, and B/C pinned DeepSeek to cheaper providers (Relace, Cloudflare) than A's per-call mix. The price swing is not a caching effect.

**Robustness.** An answer-level bootstrap (4,000 resamples) gives these 95% intervals:
- **Key (A → B):** GPT-6 Luna's new-session call 1 is +98 pts [97, 99]. GLM's new-session call 1 is +47 pts [18, 72]. DeepSeek's hit rate is +14 pts [3, 27], and its input multiplier falls 0.14 [0.03, 0.26].
- **Compaction off (B → C):** DeepSeek gains +8 pts [1, 15]. GPT-6 Luna's input multiplier falls 0.15 [0.00, 0.37]. Haiku's falls 0.10 [−0.02, 0.21], which is not significant at answer level.

Answers vary ~10× in size, hence the wide intervals. The per-event accounting in §5.3 is the lower-variance view of compaction.

**Sequential-arm caveat.** Arms ran one after another (43, 46 and 40 minutes), so each arm is also a draw of *which* provider served it:
- DeepSeek ran on a per-call mix in A, mostly Relace in B and mostly Cloudflare in C.
- GLM ran on a mix in A, mostly Together in B and only CoreWeave in C.

Those providers' own cache quality differs materially (§5.2). Provider cache quality *is* part of the caching mechanics, so for DeepSeek and GLM the A → B → C deltas mix the intended change with that draw. Only the GPT-6 Luna and Haiku rows, served by one provider in every arm, isolate the change cleanly.

**Other observations.**
- DeepSeek left 3, 6 and 5 of 24 answers empty or timed out in A, B and C. GLM left 5, 0 and 0. Haiku and Luna completed every answer. Nothing ties the DeepSeek failures to an arm (notebook 06 saw 4/24).
- Median answer latency did not move materially, apart from GLM: 31 s, 21 s, 11 s.
- Answers in C averaged fewer calls for Luna (5.0 → 4.2) and Haiku (4.75 → 4.25). This may be because superseded passages keep their chunk labels when compaction is off, but the sample cannot separate it from trajectory noise.

## 5. Findings and recommendations

### 5.1 Loss 1: the OpenAI key partitions the cache. Use one opaque key per user.

The direct probe (§3) and arm B agree. With a shared key, a new GPT-6 Luna session re-read a median 1,175 of its 1,221-token first prompt (98% token-weighted), against 0% with the session key (95% CI of the change [97, 99] pts). Its first call costs 76% less at first-party prices ($0.181 → $0.043 per thousand calls). Within a session nothing changed: calls 2+ ran at 65% / 64%, and median prefix reuse stayed 100%. Arm B keyed by tenant, but the eval stack has one tenant and one user, so the measurement carries over unchanged to the per-user key decided here.

**Why per user, not per tenant (INV-1, INV-2).**
- **The oracle.** Provider caches are content-addressed. The cached-token counts a requester sees in `done.usage`, and their latency, therefore reveal whether an identical opening was recently sent on the same partition: same system prompt, same custom instructions, same first question.
- **What it cannot reach.** Passage-bearing prefixes are out of reach. Every tool result follows an assistant message with provider-generated tool-call ids and model-chosen arguments that another user cannot reproduce. Before the first tool call, the prefix holds only session-specific content: prior assistant turns that may quote passages, and the rolling summary. Reaching those means reproducing the victim's whole session history verbatim. Evidence carry-forward contributes document and chunk ids only, never passage text. So no retrieved content (INV-2) is exposed.
- **What it can reach.** A colleague's *question* is exposed if the partition is the tenant. Least exposure (AGENTS.md §4) puts the partition at (tenant, user). That is consistent with OpenAI's guidance to key per customer or user against cache-hit probing.
- **The cost.** Each user's first chat per cache lifetime re-writes the ~1–2.4k-token system + tools head. Every later chat of that user is warm.
- **Residual.** Upstreams with no partition key (Anthropic, DeepSeek, Z.AI, Gemini) share their caches by content across all of a provider's customers, so the same probe exists there today. Tracked in [#769](https://github.com/k-sandhu/lumen-copilot/issues/769).

**Why a keyed hash, and why its own secret.** A plain `sha256(id)` is only as opaque as the id's entropy, and it is identical across deployments that share an id. The scope is therefore an HMAC-SHA256 under a **dedicated** deployment secret, `CHAT_PROMPT_CACHE_SCOPE_SECRET`, computed with the standard library.
- **Fail-fast.** Like `JWT_SECRET`, it must be set outside `local`. In `local`, a public dev default makes digests recomputable, which is acceptable for a dev stack only.
- **Why not the vault key.** Deriving the scope from `SECRETS_ENCRYPTION_KEY` was considered and rejected: `app.core.crypto` is that AES-GCM key's sole holder, with `secrets_service` as its sole caller. Reuse would widen that contract, and it would couple cache scopes to a key whose rotation is not yet supported. `crypto` has one key version, so changing the key today breaks vault decrypts.
- **Rotation.** Rotating the cache secret, or bumping the `v1` label, only costs each user one cold prefix.

**Why not finer.** A per-assistant or prompt-hash component would only re-partition one user's own warm cache, because the provider already separates different prefixes by content.

The overall effect on Luna's per-token price is small (0.51 → 0.51), because call 1 is ~1.2k tokens of a ~15k-token answer. The win is per new session: ≈ $0.14 per thousand new sessions, about 3.5% of a mean answer's cost and proportionally more for short answers.

**Recommendation:** ADR-0016 §2 amendment (#760), item 1. Implementation: [#763](https://github.com/k-sandhu/lumen-copilot/issues/763).

### 5.2 Loss 2: provider dispersion. Send a per-conversation `session_id`; do not pin providers.

- **No upstream directive exists for DeepSeek or Z.AI.** Both cache automatically, so nothing like `cache_control` is needed or available.
- **Where the misses come from.**
  - In arm A, mid-answer provider hops carried 2.9% (DeepSeek, 3 calls) and 5.3% (GLM, 6 calls) of prompt tokens, read at 0–6% cached.
  - The larger cause is the serving provider's own cache. Among calls 2+ that stayed on one provider, the zero-cache rate was 11–21% on CoreWeave, 0–10% on Together, 7–14% on Parasail and 2–5% on Relace (DeepSeek). StreamLake had none, Friendli 57%, and GMICloud and Relace (GLM) 100% on few calls.
  - That ceiling belongs to the serving provider's cache, not to the route.
- **What arm B (tenant-wide `session_id`) can and cannot show.**
  - *Not attributable:* DeepSeek's hit rate rose from 47% to 62% and its zero-cache calls 2+ fell from 12% to 3%. GLM's zero-cache calls fell from 28% to 13%. But the arms ran sequentially, and B drew different sticky providers (§4 caveat). These gains are not attributable to stickiness.
  - *Consistent with the mechanism, but too few to attribute:* GLM had 6 hop calls in A (5.3% of tokens) and 2 in B (2.7%). DeepSeek had 3 in A (2.9%) and 4 in B (4.3%), its B hops all following provider errors.
  - *Also observed:* new-session call 1 warmed (DeepSeek 55% → 67%, GLM 12% → 60%), but first calls of new sessions are only 0.8–2.6% of their input tokens. Follow-up messages' first calls read 24% → 56% and 7% → 30%.
- **Stickiness held across sessions** under the tenant-wide key. Haiku, Luna and GLM-in-C each stayed on one provider for the whole ~45-minute run. DeepSeek (B: 4 hand-overs, C: 1) and GLM-in-B (3) changed provider only mid-answer, never after an idle gap; the longest gap before a switch was 18 s. That fits OpenRouter's re-route after a provider error, and DeepSeek also had the most failed answers.
- **Granularity decided: per conversation.**
  - A tenant- or user-wide sticky key adds only the call-1 warmth of new sessions on multi-provider upstreams. Measured across all new sessions in arms A and B, that is 0.8–2.6% of input tokens: 0.80–2.48% excluding each arm's warm-up session, and 0.86–2.65% including it.
  - In exchange, it pins a busy tenant indefinitely: it is never 10 minutes idle. It would sit on one provider out of a $0.014–$0.60 per M (~40×) price spread, possibly a poor cacher, and it defeats OpenRouter's per-conversation load balancing.
  - The single-provider-in-practice routes need no wide key: Luna and Haiku started all 16/16 sessions (24 answers) per arm on one provider without one.
  - A per-conversation `session_id` keeps the in-answer stickiness that arm B exercised, since a tenant-wide and a per-conversation key are equally constant within one answer. It pins from a conversation's first call instead of after its first observed hit, and it is Z.AI's affinity key.
- **Not measured.** An arm D with a per-conversation `session_id` was prepared on 2026-10-15 but not run, because the shared `lumen-eval` stack had been recreated from another checkout for a different evaluation. Running the workload there would have contaminated both. No request was sent, and the eval checkout was restored. [#763](https://github.com/k-sandhu/lumen-copilot/issues/763)'s live smoke verifies that one answer's calls stay on one provider.
- **Cost/benefit.** `session_id` is free; it rides `extra_body` through the pinned LiteLLM.
  - Rejected alternative: `provider.order`. It disables sticky routing, hard-codes vendors in Lumen and loses OpenRouter's price/availability routing.
  - If GLM-class routes matter commercially, an *operator-level* per-model provider allow-list in the model registry is the place for it, as a deployment concern with its own issue. It does not belong in the cache key.

**Recommendation:** ADR-0016 §2 amendment (#760), item 2. Implementation: [#763](https://github.com/k-sandhu/lumen-copilot/issues/763).

### 5.3 Loss 3: proactive compaction is cost-negative on caching routes. Turn it off by default.

**What a proactive compaction saves.** Measured on 286 events across arms A and B, it saved a median **84–178 tokens** (Luna 88–93, Haiku 84–89, GLM 141–178, DeepSeek 169–178), about 0.4% of a ~20–45k-token prompt. The cause: every retrieved passage is a recorded citation, and the digest re-embeds every cited snippet verbatim (§3).

**What it costs.** The next call loses a median **6.8–8.4k** previously cached tokens (`prev_prompt − cached`, net of the no-compaction baseline loss). Those are re-billed at the uncached rate, or at 1.25× as cache writes on Anthropic and GPT-5.6+.

**Per-event accounting** (first-party ratios; the saving is counted as cache reads on every later call of the answer):

| Model | Calls after a compaction | Re-billed cached tokens X (median) | Tokens saved δ (median) | Later calls (mean) | Later calls needed to break even, n > (w − r)·X / (r·δ) | Net cost per event, mean (input-token equivalents) |
|---|---|---|---|---|---|---|
| Haiku 5.5 (r 0.1, w 1.25) | 50 | 8,435 | 88 | 4.2 | ~1,100 | +17,100 |
| DeepSeek V4.1 Flash (r 0.02, w 1) | 114 | 7,396 | 171 | 4.2 | ~2,100 (~390 at r = 0.1) | +10,800 |
| GLM-5.3 Flash (r 0.2, w 1) | 35 | 7,801 | 163 | 2.7 | ~190 | +6,900 |
| GPT-6 Luna (r 0.1, w 1.25) | 77 | 6,837 | 90 | 5.9 | ~870 | +10,500 |

The break-even column applies the formula to the medians in its own row. The net-cost column is the mean, over events, of (w − r)·X − r·δ·(1 + later calls). The earlier draft's ~175–1,810 break-evens were ratios of per-model *totals*. They are superseded by these formula-consistent medians, with the same conclusion.

Turning it off (arm C) raised the hit rate 8–12 points on Haiku, DeepSeek and Luna, and cut the effective input price 18–29%. GLM did not move, since its misses are provider-side (§5.2).

**The two proposed refinements do not change the sign.** A digest that saves δ tokens and invalidates X cached tokens pays back after *n* later calls only when n · r · δ > (w − r) · X, with r ≈ 0.02–0.2 and w = 1–1.25.
- **"Compact only beyond the last cached breakpoint."** On an implicit cache, *everything* already sent is cached. The only uncached content is the newest tool group, which AC-6 forbids compacting, so the rule reduces to "never in-answer".
- **"Larger, rarer steps."** This one is a closer call, and an earlier draft overstated it. In this runtime the per-call path compacts the group just before the newest one, so its rewritten suffix is only that digest plus one assistant tool-call message; the newest group is new content anyway. Batching m groups into one step re-bills the m digests and the m assistant messages once. That is about the same volume, so it saves at most the per-step framing. What it cannot change is the per-group term: each digest is new prefix content billed at w, while its verbatim original would have been read at r. That term carries the break-even. With δ ≈ 90–170, batching every group of an answer into one step still needs far more later calls than the 50-turn ceiling allows. It also forgoes the (tiny) savings while it defers.
- **An honest shrink still loses.** Suppose a digest that dropped the verbatim re-embedding, with δ ≈ X − 300. It still needs n > ~12 later calls at r = 0.1, while an answer that reaches call 3 makes ~4 more on average (notebook 06).
- **Long loops.** Later calls are bounded by the tool-turn budget: `CHAT_MAX_TOOL_TURNS` defaults to 20, and both it and the per-tenant admin override are validated to at most 50, plus 1 forced synthesis. Every measured break-even (~190–2,100) is far above even 50, so no permitted loop length pays a digest back. The workload averaged 4–6 calls, so loops near the bound were not exercised. This rests on the arithmetic, not on observation.
- **Routes that do not cache.** There a digest has no cache to forfeit, and proactive compaction saves exactly δ per later call: a median 0.37–0.88% of the prompt here. Default-off therefore costs a non-caching route under ~1% of input tokens. A deployment whose routes are *all* non-caching can opt back in. A **mixed** deployment cannot be served by one global flag. A route-aware switch would belong inside `llm/` (the route-capability map), but is not worth building for < 1% unless a non-caching route shows a materially larger δ.

In-answer compaction remains correct as a **budget** tool: the reactive, over-budget, chunked path of §3.1. It is not a cost tool on a caching route.

**Recommendation:** ADR-0016 §3.1 amendment (#760). `CONTEXT_PROACTIVE_COMPACTION_ENABLED` defaults to false. The reactive path is unchanged. The offline prompt-economics harness must model runtime citation semantics and cache pricing (§6) before it justifies a compaction change again. Implementation: [#764](https://github.com/k-sandhu/lumen-copilot/issues/764).

### 5.4 Settled in passing: the `tools` array is inside the cached prefix

`docs/runbooks/prompt-cache-kpi.md` listed this as an open empirical question. On Haiku, a new session with a **different** question re-read 2,242 of 2,375 call-1 tokens; only 133, the question, were uncached. The default tool schemas are ~950 tokens of that prompt, so the cached prefix must include them. GLM (1,472 of 1,566) and GPT-6 Luna (1,175 of 1,221) show the same under arm B/C.

## 6. Residual risk and what was not done

- **Groundedness was not judged live.** Turning proactive compaction off only keeps *more* verbatim, labelled evidence in view. The named gate still blocks the default change (#764): the ADR-0016 compression-regression eval, `backend/tests/eval/test_eval_compression.py`.
- **Single tenant, low traffic.** The eval stack has one tenant and ≤ 4 concurrent conversations. Per-key overflow at high volume was not exercised. Sharding the key per session was deferred, not adopted: OpenAI's ~15 requests/minute-per-key guidance is for pre-GPT-5.6 routing, and on GPT-5.6+ the key partitions the cache, so shards would multiply cold prefixes.
- **Provider draw.** B and C each landed on one sticky provider per model, and providers differ in both price *and* cache quality. A different first pick would change billed cost and, for DeepSeek and GLM, the hit-rate and multiplier rows too (§4 sequential-arm caveat). Only the single-provider Luna and Haiku rows isolate the intended changes cleanly.
- **The offline harness overstates compaction.** `backend/tests/test_prompt_economics.py` (and #658's 25.5% figure) models one 200-character cited snippet per result. The runtime cites every retrieved passage in full, so the harness's token reduction does not occur live (≈0.4%). Its realism fix is part of the compaction follow-up.
- **Per-conversation stickiness was not measured** (§5.2, arm D). The decision rests on arm A/B attribution and on #763's live smoke.
- **A cache-probing oracle remains on unpartitioned upstreams** (§5.1). See [#769](https://github.com/k-sandhu/lumen-copilot/issues/769).
- **The ledger misses cache writes** (#759). Write tokens here come from the raw provider usage, not Lumen's `llm_usage`.

## 7. Reproduce

1. The eval-only patches are archived with this spike's working notes; they are not in the repo:
   - `gateway.py`: the arm switch, the shared-scope `session_id` / `prompt_cache_key` (keyed by the tenant id in arms B and C, so a plain hash was enough for the eval), and the `eval.llm_call_raw` line.
   - `chat_runtime.py`: the tenant id as the cache key in arms B and C.
   - `context.py`: the arm-C compaction switch, plus `saved_tokens` / `before_tokens` on the compaction line.
2. The harness is notebook 06's client and questions plus `GET /api/v1/generation` per call: `run <arm>` per arm, then `harvest`.
3. Totals: arm A, B and C took 43, 46 and 40 minutes. 1,406 generation records were fetched, and the agent-loop calls cost $1.57 as billed by OpenRouter.
