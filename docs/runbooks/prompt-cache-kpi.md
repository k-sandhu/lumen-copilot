# Runbook — the prompt-cache KPI, per route family (#491)

> **What this documents.** How to read the `llm.cache_kpi` log series the chat
> answer path emits, what a cache hit means per **route family**, and — the part
> operators keep getting wrong — **which route families get no Lumen cache
> directive**. A zero or a low KPI on those routes is therefore never a
> regression, though the provider's own implicit caching may still produce hits. Governed
> by [ADR-0016](../architecture/0016-context-engine-and-cache-first-prompting.md)
> §2.4 (cache directives) and §2.6 (usage accounting).

## The KPI

Every **successful** chat answer emits exactly one structured log line
(`app/services/chat_runtime.py::_log_cache_kpi`), after the DB commit and the
terminal `done`:

```
event = llm.cache_kpi
  cached_prompt_tokens   # prompt tokens the provider served from its cache
  prompt_tokens          # total prompt (input) tokens billed for the answer
  cache_hit_ratio        # round(cached / prompt, 3), or null when unreported
  cache_write_tokens     # prompt tokens written INTO the cache this answer
  usage_reported         # false when the route returned no usage at all
```

- The series is a **denominator over all successful answers** — a route that
  reports no usage still emits (`usage_reported=false`, null ratio), so the
  routes operators most need to see are never silently dropped.
- Counts only. No prompt text, ids, or secrets ever enter the line.
- Numbers come from the provider's own `usage` block (`cached_prompt_tokens` /
  `cache_write_tokens` on `TokenUsage`), summed across every route scope an
  answer used (primary + any failover), so `cache_hit_ratio` is a per-answer
  figure, not per-turn.

**Reading it.** A healthy multi-turn answer on a cacheable route shows
`cache_hit_ratio` climbing toward the fraction of the prompt that is stable
prefix (system + tools + prior turns) — turn 1 writes, every later turn reads.
The first turn of a fresh session is a pure write (`cached_prompt_tokens` ≈ 0,
`cache_write_tokens` > 0); that is expected, not a miss.

## Preserve cached prefixes: proactive compaction defaults off (#764)

`CONTEXT_PROACTIVE_COMPACTION_ENABLED=false` is the default. Leave it off on
caching routes: rewriting a superseded tool result invalidates the cached prefix
from that result onward. The [#760 live spike](../audits/prompt-cache-scope-spike-2026-10-15.md)
measured little raw-token saving but thousands of lost cache-read tokens per
rewrite. Enable the flag only for a deployment whose routes do **not** cache.
The global flag does not adapt to mixed routes. Existing explicit `true`
overrides remain effective; remove them or set `false` to adopt the new default.

Budget-threshold reactive compaction remains active: an over-budget transcript
still compacts in chunks, uncited results first, preserving cited snippets
verbatim. The proactive mechanism remains available behind the flag with the
same evidence protection. Neither path changes what constitutes a citation.

The deterministic `backend/tests/test_prompt_economics.py` harness now protects
every complete retrieved passage using `rendered_snippet`, matching runtime
citation semantics. This supersedes [#658](https://github.com/k-sandhu/lumen-copilot/issues/658)'s
legacy ceiling question: the 8k–20k raw-token guard remains, but the former
10% savings requirement based on 200-character evidence substitutes is removed.
It prints raw tokens, cached reads, uncached/written tokens and normalized cost
for both policies. Pricing assumes 0.1x cache reads and 1.25x uncached/cache
writes, complete identical message-prefix reuse, and no suffix reuse after a
rewrite. It excludes TTL/eviction, provider block rounding and partial-message
prefix reuse; these are modeled input-cost units, not provider billing claims.
The separate rewrite probe verifies that a small raw-token saving can increase
priced cost by invalidating the later suffix.

Measured offline with the pinned tokenizer: the three-search workload sends
17,292 cumulative input tokens under either policy (10,305 cached reads,
6,987 uncached/written, 9,764.25 normalized cost units). Complete evidence leaves
no profitable digest in this fixture; it demonstrates **zero** policy saving,
rather than the former 10% claim. In the separate wrapper-rewrite probe, all
cited blocks remain complete: removing 33 raw tokens drops cached reads from
7,020 to 1,716 and raises modeled cost from 9,477 to 15,535.35 units (+6,058.35).
This is a deterministic invalidation example, not a measured provider invoice.

Run from `backend/`:

```powershell
uv run --extra dev pytest tests/test_prompt_economics.py -n 0 -o addopts= -q -s
uv run --extra dev pytest tests/eval/test_eval_compression.py -n 0 -o addopts= -q
```

The compression-regression eval is the **blocking groundedness gate**; the
runtime prefix regression and any live notebook accuracy results supplement it
and never replace it. Its default-policy case keeps the superseded result's
passage labels and evidence byte-identical. The existing positive/negative
reactive-compaction grounding cases remain in place.

## Per route family

Cache directives are decided **inside the gateway**, keyed off the *resolved
route* — `(model id, api_base)` — never a model-name substring
(`app/llm/gateway.py::_cache_family`). Directives are granted **only to routes
demonstrably going through OpenRouter** (the one transport the pass-through was
live-validated against — ADR-0016 §2.4). The kill-switch
`CHAT_PROMPT_CACHE_ENABLED` (process-static) turns all of this off.

| Route family (OpenRouter upstream prefix) | Directive emitted | Expected KPI |
|---|---|---|
| `openrouter/anthropic/…` (e.g. Claude Opus/Haiku — the **default chat route**) | unchanged `cache_control` breakpoints on message 0 + the last cacheable block; opaque per-conversation `extra_body.session_id` | **> 0 from turn 2 on**; ratio climbs as the transcript grows |
| `openrouter/openai/…` (e.g. GPT-5.5) | opaque per-user `extra_body.prompt_cache_key` plus per-conversation `extra_body.session_id` (messages untouched) | **> 0 once the stable prefix ≥ the provider minimum** (OpenAI auto-caches ≥1024-token prefixes); provider-dependent |
| `openrouter/google/…` (Gemini) | opaque per-conversation `extra_body.session_id` only | provider-implicit: Gemini 2.5+ caches automatically, so **> 0 is normal** |
| `openrouter/deepseek/…`, `openrouter/z-ai/…` (DeepSeek, GLM) | opaque per-conversation `extra_body.session_id` only | provider-implicit: **> 0 is normal**, but erratic. These upstreams are served by 30+ OpenRouter providers, each with its own cache. Measured on 2026-10-14/15: 47% / 41% answer hit, with 12% / 28% of calls 2+ at zero cache ([#760 spike](../audits/prompt-cache-scope-spike-2026-10-15.md)) |
| `openrouter/qwen/…` (Qwen) | opaque per-conversation `extra_body.session_id` only | provider-dependent: Alibaba-served Qwen caches only with explicit `cache_control`; other providers may cache implicitly |
| any **non-OpenRouter** `api_base` (a tenant's own OpenAI-compatible endpoint) | **none — fail-safe**, even when the model is *named* `claude`/`gpt` | whatever the endpoint caches implicitly (often 0) |
| any id **without** the `openrouter/` adapter prefix and no OpenRouter `api_base` | **none — fail-safe** | whatever the native provider caches implicitly (often 0) |
| kill-switch `CHAT_PROMPT_CACHE_ENABLED=false` | **none** | no Lumen directives anywhere; implicit hits may still appear on upstreams whose providers cache automatically (DeepSeek, GLM, Gemini; OpenAI with no key) |

**Transport affinity is not a cache partition.** Every approved OpenRouter
transport receives the per-conversation opaque `session_id` when all domain ids
are present. Google, DeepSeek, Z.AI, Qwen and other upstreams receive no upstream
cache directives: hits remain provider-dependent. Non-OpenRouter transports,
missing tenant/user/session identity and the kill-switch-off case receive neither
routing hints nor breakpoints. Implicit provider hits may still occur; zero is
not by itself a regression on these routes.

**Implemented two-level scope (#763, accepted ADR-0016 amendment).** The runtime
passes only canonical tenant/user/session UUIDs. Inside `llm/`,
`CHAT_PROMPT_CACHE_SCOPE_SECRET` derives purpose-separated HMAC-SHA256 values:
`lc1u-` plus 24 hex digits for `(tenant,user)` and `lc1s-` plus 24 hex digits for
`(tenant,session)`, using the `v1:user:` / `v1:session:` UTF-8 messages. Only the
OpenAI family receives the per-user partition, so new sessions can reuse that
user's stable head without sharing the partition with colleagues. Every approved
OpenRouter family receives session affinity from call one. Provider failover or
security changes can still invalidate a prefix; affinity is not a guarantee of
cache quality or provider availability. The unpartitioned-upstream probing
residual remains tracked by #769. Proactive compaction remains off by default
(#764); neither model/provider selection nor retry/latency budgets change here.

Live proof is explicit and key-gated. Run the dedicated suite **alone in a fresh
process** (its isolated SDK cleanup must not affect another active loop):

```powershell
$env:RUN_LIVE = '1'
uv run --extra dev pytest tests/test_prompt_cache_scope_live.py -n 0 -o addopts= -q -s
```

The first proof sends three synthetic GPT-6 Luna calls: one user's first session,
their second session with a different question (requires cached tokens >0), then
a different user's identical opening (requires zero). The second forces two
synthetic DeepSeek lookup turns plus synthesis with one conversation identity;
OpenRouter generation records must attribute all three calls to one serving
provider. These bounded fixture route choices do not edit application settings.
Missing model support or failed live evidence is a failure, never a waived gate.
Keep generation identifiers and raw provider diagnostics private; report token
counts, call counts and provider-equality aggregates only. The live spy retains
per-call identifiers privately before metadata lookup, preferring router `gen-`
identifiers over upstream `chatcmpl-` identifiers. Attribution uses the
[OpenRouter generation endpoint](https://openrouter.ai/docs/api/api-reference/generations/get-request-%26-usage-metadata-for-a-generation),
whose `data.id` and `upstream_id` are distinct. A single bounded 60-second metadata
visibility window uses short HTTP timeouts; missing records remain failed proof,
not evidence of provider equality. Offline observer tests verify chunk identity,
private per-call persistence and upstream-only rejection.

**Beyond the answer loop.** Only `stream_tools` (the answer loop) emits
directives. The follow-up-suggestions completion, the `/search` direct answer,
and the rolling summarizer call `gateway.chat`, which has no cache path — those
spends are cache-cold by construction and do not appear in this KPI (it is a
per-*answer* series). #490 moved suggestions and summaries onto a dedicated FAST
route, so their cache-coldness is cheap.

## Settled (2026-10-15, #760): the `tools` array IS inside the cached prefix

On `openrouter/anthropic/claude-haiku-5.5`, a new session asking a **different**
question re-read 2,242 of its 2,375 call-1 prompt tokens. Only the question
(~133 tokens) was uncached, and the ~950-token default tool schemas are part of
that prompt. GLM and GPT-6 Luna show the same under a shared key. The analysis
below is kept for its reasoning about *why* the code alone could not settle it.

## Formerly open — is the `tools` array inside the cached prefix?

**Statically (what the repo does):** No. `_apply_cache_directives`
(`app/llm/gateway.py`) rewrites entries of the serialized **`messages`** list
only — index 0 and the last wrappable block. The `tools` array is a *sibling*
request parameter (`tools=self._to_wire_tools(tools)`); nothing in this repo
attaches a `cache_control` field to it, marks it, or asserts it is cached. The
context assembler counts the tools block only as budget **spend**
(`_tools_wire_text`), never as a marked prefix. So by our code, the ~950-token
tool block is **not** something we place inside a cached prefix.

**Empirically (what we cannot settle from the code):** On Anthropic's *native*
API the tools block precedes system/messages, so a breakpoint at message 0
covers it implicitly — but we route through **OpenRouter**, whose request
normalization sits between us and Anthropic. Whether OpenRouter re-emits our
message-0 `cache_control` such that the tool block lands inside the cached
prefix is **not observable from usage counts alone** (the provider reports one
`cached_prompt_tokens` figure for the whole prefix, not a per-segment
breakdown). It is therefore an **open empirical question**, not a settled fact.

**Consequence for tuning.** If a future measurement shows the tool block is
*not* cached and its ~950 tokens are a material re-send cost, the fix is a
message-assembly restructuring that brings the stable prefix (system + tools)
under an explicit breakpoint — an **ADR-0016 §2 change** (a superseding ADR),
not an implementation tweak. Until such a measurement exists, we do not guess:
the live smokes (`test_live_prompt_cache_tool_loop_reads_cache`,
`test_live_prompt_cache_session_shaped_reads_on_turn_two` in
`backend/tests/test_llm_gateway.py`) prove turn-2 cache reads on the real
Anthropic route; they deliberately do **not** claim the tool block is among the
cached tokens.

## How to run the live verification

The cache smokes are gated exactly like every other live test (offline-safe):

```
RUN_LIVE=1 OPENROUTER_API_KEY=<key> \
  uv run --extra dev pytest -q backend/tests/test_llm_gateway.py -k prompt_cache
```

They cost a few small Haiku calls. A turn-2 `cached_prompt_tokens == 0` on the
Anthropic route is a real regression (the moving mark or the message-0
breakpoint was dropped somewhere in the stack). A 0, or any value, on a
no-directive route is not.
