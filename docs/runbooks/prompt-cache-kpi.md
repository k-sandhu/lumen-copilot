# Runbook — the prompt-cache KPI, per route family (#491)

> **What this documents.** How to read the `llm.cache_kpi` log series the chat
> answer path emits, what a cache hit means per **route family**, and — the part
> operators keep getting wrong — **which route families report 0 hits by
> design** so a zero on those routes is never mistaken for a regression. Governed
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

## Per route family

Cache directives are decided **inside the gateway**, keyed off the *resolved
route* — `(model id, api_base)` — never a model-name substring
(`app/llm/gateway.py::_cache_family`). Directives are granted **only to routes
demonstrably going through OpenRouter** (the one transport the pass-through was
live-validated against — ADR-0016 §2.4). The kill-switch
`CHAT_PROMPT_CACHE_ENABLED` (process-static) turns all of this off.

| Route family (OpenRouter upstream prefix) | Directive emitted | Expected KPI |
|---|---|---|
| `openrouter/anthropic/…` (e.g. Claude Opus/Haiku — the **default chat route**) | `cache_control` breakpoints on message 0 + a moving mark on the last cacheable block | **> 0 from turn 2 on**; ratio climbs as the transcript grows |
| `openrouter/openai/…` (e.g. GPT-5.5) | `extra_body.prompt_cache_key = session_id` (implicit caching, messages untouched) | **> 0 once the stable prefix ≥ the provider minimum** (OpenAI auto-caches ≥1024-token prefixes); provider-dependent |
| `openrouter/google/…` (Gemini) | **none** | **0 by design** |
| `openrouter/deepseek/…` (DeepSeek) | **none** | **0 by design** |
| `openrouter/qwen/…` (Qwen) | **none** | **0 by design** |
| any **non-OpenRouter** `api_base` (a tenant's own OpenAI-compatible endpoint) | **none — fail-safe** | **0 by design**, even when the model is *named* `claude`/`gpt` |
| any id **without** the `openrouter/` adapter prefix and no OpenRouter `api_base` | **none — fail-safe** | **0 by design** |
| kill-switch `CHAT_PROMPT_CACHE_ENABLED=false` | **none** | **0 everywhere** |

**0-by-design routes (do not page on these):** every `google/`, `deepseek/`,
`qwen/` upstream; every non-OpenRouter transport; and the kill-switch-off case.
For these the pass-through was never validated, so the gateway emits nothing
rather than send a directive a transport might reject — a route's model *name*
never earns directives its *transport* wasn't proven to accept.

**Beyond the answer loop.** Only `stream_tools` (the answer loop) emits
directives. The follow-up-suggestions completion, the `/search` direct answer,
and the rolling summarizer call `gateway.chat`, which has no cache path — those
spends are cache-cold by construction and do not appear in this KPI (it is a
per-*answer* series). #490 moved suggestions and summaries onto a dedicated FAST
route, so their cache-coldness is cheap.

## Open question — is the `tools` array inside the cached prefix?

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
breakpoint was dropped somewhere in the stack); a 0 on any 0-by-design route is
not.
