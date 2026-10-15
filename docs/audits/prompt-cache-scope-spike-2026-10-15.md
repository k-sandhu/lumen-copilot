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
- `session_id` is also Z.AI's session-affinity key. DeepSeek and Z.AI cache automatically (DeepSeek: write 1×, read ~0.1×; Z.AI: read ~0.2×).

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

¹ Σ(uncached × 1 + cached × read ratio + written × write ratio) / Σ prompt tokens, priced at the **first-party** provider's ratios (Anthropic/OpenAI read 0.1×, write 1.25×; DeepSeek read 0.02×; Z.AI read 0.2×). 1.00 means "every prompt token at full price". This isolates caching from which provider a sticky key happened to land on, and from trajectory length.
² What OpenRouter billed. The DeepSeek and GLM figures are dominated by the serving provider's price. Prompt prices for one model span $0.014–$0.60 per M, and B/C pinned DeepSeek to cheaper providers (Relace, Cloudflare) than A's per-call mix. The price swing is not a caching effect.

**Robustness.** An answer-level bootstrap (4,000 resamples) gives these 95% intervals:
- **Key (A → B):** GPT-6 Luna's new-session call 1 is +98 pts [97, 99]. GLM's new-session call 1 is +47 pts [18, 72]. DeepSeek's hit rate is +14 pts [3, 27], and its input multiplier falls 0.14 [0.03, 0.26].
- **Compaction off (B → C):** DeepSeek gains +8 pts [1, 15]. GPT-6 Luna's input multiplier falls 0.15 [0.00, 0.37]. Haiku's falls 0.10 [−0.02, 0.21], which is not significant at answer level.

Answers vary ~10× in size, hence the wide intervals. The per-event accounting in §5.3 is the lower-variance view of compaction.

**Other observations.**
- DeepSeek left 3, 6 and 5 of 24 answers empty or timed out in A, B and C. GLM left 5, 0 and 0. Haiku and Luna completed every answer. Nothing ties the DeepSeek failures to an arm (notebook 06 saw 4/24).
- Median answer latency did not move materially, apart from GLM: 31 s, 21 s, 11 s.
- Answers in C averaged fewer calls for Luna (5.0 → 4.2) and Haiku (4.75 → 4.25). This may be because superseded passages keep their chunk labels when compaction is off, but the sample cannot separate it from trajectory noise.

## 5. Findings and recommendations

### 5.1 Loss 1: the OpenAI key partitions the cache. Use one opaque key per tenant.

The direct probe (§3) and arm B agree. With a tenant-scoped key, a new GPT-6 Luna session re-read a median 1,175 of its 1,221-token first prompt (98% token-weighted), against 0% with the session key. Its first call costs 76% less at first-party prices ($0.181 → $0.043 per thousand calls). Within a session nothing changed: calls 2+ ran at 65% / 64%, and median prefix reuse stayed 100%. The scope is the tenant, never wider:
- OpenAI documents per-customer keys as the guard against cross-customer cache-hit probing (INV-1).
- Within a tenant, every chat shares the grounded prompt and tool definitions.
- A per-assistant or prompt-hash key would only re-partition one tenant's own warm cache, because the provider already separates different prefixes by content.

The overall effect on Luna's per-token price is small (0.51 → 0.51), because call 1 is ~1.2k tokens of a ~15k-token answer. The win is per new session: ≈ $0.14 per thousand new sessions, about 3.5% of a mean answer's cost and proportionally more for short answers.

**Recommendation:** ADR-0016 §2 amendment (#760), item 1. Implementation: [#763](https://github.com/k-sandhu/lumen-copilot/issues/763).

### 5.2 Loss 2: provider dispersion. Send OpenRouter's `session_id`; do not pin providers.

- **No upstream directive exists for DeepSeek or Z.AI.** Both cache automatically, so nothing like `cache_control` is needed or available.
- **The lever is stickiness.** With `session_id` = tenant scope, DeepSeek's zero-cache calls 2+ fell from 12% to 3% and its hit rate rose from 47% to 62%. GLM's fell from 28% to 13%, and its provider switches from 8% to 2%. Both models' first message in a new session now reuses the shared prefix: DeepSeek 55% → 67%, GLM 12% → 60%. Their follow-up messages read 24% → 56% and 7% → 30%.
- **Stickiness held across sessions**, as a tenant-wide key intends. In B and C, Haiku, Luna and GLM-in-C each stayed on one provider for the whole ~45-minute run. DeepSeek (B: 4 hand-overs, C: 1) and GLM-in-B (3) changed provider only mid-answer, never after an idle gap; the longest gap before a switch was 18 s. That fits OpenRouter's re-route after a provider error, and DeepSeek also had the most failed answers.
- **What stickiness cannot fix.** GLM's overall hit rate did not move (41% → 43%). Most of its remaining misses happen on the *same* provider. Among calls 2+ that stayed on one provider, the zero-cache rate was 11–21% on CoreWeave, 10% on Together and 7–14% on Parasail; StreamLake had none; Friendli 57%; GMICloud and Relace 100% on few calls. The ceiling there is the serving provider's own cache, not the route.
- **Cost/benefit.** `session_id` is free. It rides `extra_body` through the pinned LiteLLM, and OpenRouter forwards it as Z.AI's affinity key.
  - Risk: a tenant stays on whatever provider OpenRouter first picked, by price-weighted choice, until 10 minutes of idleness or an error. That provider may cache poorly (as GLM-on-Together did here) or be degraded but up. The expected price is unchanged, but it varies from one tenant to the next.
  - Rejected alternative: `provider.order`. It disables sticky routing, hard-codes vendors in Lumen and loses OpenRouter's price/availability routing.
  - If GLM-class routes matter commercially, an *operator-level* per-model provider allow-list in the model registry is the place for it, as a deployment concern with its own issue. It does not belong in the cache key.

**Recommendation:** ADR-0016 §2 amendment (#760), item 2(a). Implementation: [#763](https://github.com/k-sandhu/lumen-copilot/issues/763).

### 5.3 Loss 3: proactive compaction is cost-negative on caching routes. Turn it off by default.

**What a proactive compaction saves.** Measured on 286 events across arms A and B, it saved a median **84–178 tokens** (Luna 88–93, Haiku 84–89, GLM 141–178, DeepSeek 169–178), about 0.4% of a ~20–45k-token prompt. The cause: every retrieved passage is a recorded citation, and the digest re-embeds every cited snippet verbatim (§3).

**What it costs.** The next call loses a median **6.8–8.4k** previously cached tokens (`prev_prompt − cached`, net of the no-compaction baseline loss). Those are re-billed at the uncached rate, or at 1.25× as cache writes on Anthropic and GPT-5.6+.

**Per-event accounting** (first-party ratios; the saving is counted as cache reads on every later call of the answer):

| Model | Calls after a compaction | Re-billed cached tokens (median) | Tokens saved (median) | Later calls (mean) | Later calls needed to break even | Net cost per event (input-token equivalents) |
|---|---|---|---|---|---|---|
| Haiku 5.5 | 50 | 8,435 | 88 | 4.2 | ~1,790 | +17,100 |
| DeepSeek V4.1 Flash | 114 | 7,396 | 171 | 4.2 | ~1,810 | +10,800 |
| GLM-5.3 Flash | 35 | 7,801 | 163 | 2.7 | ~175 | +6,900 |
| GPT-6 Luna | 77 | 6,837 | 90 | 5.9 | ~880 | +10,500 |

Turning it off (arm C) raised the hit rate 8–12 points on Haiku, DeepSeek and Luna, and cut the effective input price 18–29%. GLM did not move, since its misses are provider-side (§5.2).

**The two proposed refinements do not change the sign.** A digest that saves δ tokens and invalidates X cached tokens pays back after *n* later calls only when n · r · δ > (w − r) · X, with r ≈ 0.02–0.2 and w = 1–1.25.
- **"Compact only beyond the last cached breakpoint."** On an implicit cache, *everything* already sent is cached. The only uncached content is the newest tool group, which AC-6 forbids compacting, so the rule reduces to "never in-answer".
- **"Larger, rarer steps."** Batching re-bills each superseded group once either way, plus every still-verbatim group behind the earliest rewrite, so it re-bills at least as much.
- **An honest shrink still loses.** Suppose a digest that dropped the verbatim re-embedding, with δ ≈ X − 300. It still needs n > ~12 later calls at r = 0.1, while an answer that reaches call 3 makes ~4 more on average (notebook 06).

In-answer compaction remains correct as a **budget** tool: the reactive, over-budget, chunked path of §3.1. It is not a cost tool on a caching route.

**Recommendation:** ADR-0016 §3.1 amendment (#760). `CONTEXT_PROACTIVE_COMPACTION_ENABLED` defaults to false. The reactive path is unchanged. The offline prompt-economics harness must model runtime citation semantics and cache pricing (§6) before it justifies a compaction change again. Implementation: [#764](https://github.com/k-sandhu/lumen-copilot/issues/764).

### 5.4 Settled in passing: the `tools` array is inside the cached prefix

`docs/runbooks/prompt-cache-kpi.md` listed this as an open empirical question. On Haiku, a new session with a **different** question re-read 2,242 of 2,375 call-1 tokens; only 133, the question, were uncached. The default tool schemas are ~950 tokens of that prompt, so the cached prefix must include them. GLM (1,472 of 1,566) and GPT-6 Luna (1,175 of 1,221) show the same under arm B/C.

## 6. Residual risk and what was not done

- **Groundedness was not judged.** Turning proactive compaction off only keeps *more* verbatim, labelled evidence in view, but the compression-regression eval (§3.2 / OD-7) still gates the default change.
- **Single tenant, low traffic.** The eval stack has one tenant and ≤ 4 concurrent conversations. Per-key overflow at high volume was not exercised. Sharding the key per session was deferred, not adopted: OpenAI's ~15 requests/minute-per-key guidance is for pre-GPT-5.6 routing, and on GPT-5.6+ the key partitions the cache, so shards would multiply cold prefixes.
- **Provider luck.** B and C each landed on one sticky provider per model. A different first pick would change billed cost, but not the caching mechanics measured by the hit-rate and input-price-multiplier rows.
- **The offline harness overstates compaction.** `backend/tests/test_prompt_economics.py` (and #658's 25.5% figure) models one 200-character cited snippet per result. The runtime cites every retrieved passage in full, so the harness's token reduction does not occur live (≈0.4%). Its realism fix is part of the compaction follow-up.
- **The ledger misses cache writes** (#759). Write tokens here come from the raw provider usage, not Lumen's `llm_usage`.

## 7. Reproduce

1. The eval-only patches are archived with this spike's working notes; they are not in the repo:
   - `gateway.py`: the arm switch, tenant-scoped `session_id` / `prompt_cache_key`, and the `eval.llm_call_raw` line.
   - `chat_runtime.py`: the tenant id as the cache key in arms B and C.
   - `context.py`: the arm-C compaction switch, plus `saved_tokens` / `before_tokens` on the compaction line.
2. The harness is notebook 06's client and questions plus `GET /api/v1/generation` per call: `run <arm>` per arm, then `harvest`.
3. Totals: arm A, B and C took 43, 46 and 40 minutes. 1,406 generation records were fetched, and the agent-loop calls cost $1.57 as billed by OpenRouter.
