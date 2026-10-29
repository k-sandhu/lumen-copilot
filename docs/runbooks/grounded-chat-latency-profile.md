# Grounded-chat latency investigation (#802)

This investigation preserves the configured model/provider, the five-sample
HTTP/WebSocket grounding assertions, and the original <2s median/30s error
budgets. It does not claim a performance repair or a passing gate.

## Baseline and provenance

The baseline ran on `19dec232f944b8c4dacce1e60254cd5a1be56c77` in a fresh
Python 3.12.5 process on Windows, with pinned LiteLLM 1.55.9, OpenAI 2.43.0 and
HTTPX 0.27.2. Root-owned disposable PostgreSQL16, Redis7.4 and OpenSearch2.19.1
ran under an isolated WSL engine. OpenSearch used a QA-only loopback forward;
the authenticated ASGI HTTP/WebSocket client ran in-process over real Redis.
A quiet slot excluded other builds, suites and provider probes.

The existing default chat registry entry was
`openrouter/inclusionai/ling-3.0-flash`; embeddings used
`openai/nvidia/nemotron-3-embed-1b:free`, 2048 dimensions. Interactive timeout
was 25s, provider timeout 60s, prompt caching enabled. No model/provider setting
was overridden. These are this run's configured routes, not a measurement of
the runbook's historical Haiku example. Upstream serving region and network
conditions were not controlled or inferred.

The external timing-only pytest plugin wrapped gateway stream/embedding,
retrieval, runtime phase and publish boundaries, plus the existing per-answer
harness helper. It retained original calls and assertions. It recorded no
prompts, passages, vectors, credentials, cache keys or provider error text.
Its SHA256 was
`573360cdbd6640d8497ffa2cd1c9de7451225cc7fecc09f42df336200e2ec8d2`.
The [safe baseline artifact](../../qa/reproductions/profiles/802-baseline.json)
retains every sample, stage duration and runtime context. Raw private diagnostics
must not be pasted into issues or PRs.

| Sample | Process | Client TTFT | Model tool-selection turn | Retrieval (embedding included) | Model synthesis turn |
|---|---|---:|---:|---:|---:|
| 1 | cold | 5.589s | 3.839s | 0.675s (0.358s) | 1.050s |
| 2 | warm | 2.802s | 1.441s | 0.037s (0.001s) | 1.321s |
| 3 | warm | 2.405s | 1.097s | 0.078s (0.001s) | 1.228s |
| 4 | warm | 2.574s | 1.210s | 0.040s (0.002s) | 1.281s |
| 5 | warm | 2.852s | 1.179s | 0.077s (0.001s) | 1.572s |

All five passed the unchanged exactly-one retrieval/nonempty passage/citation
checks. Median TTFT was **2.802s: FAIL** against <2.000s. Model-turn durations
include their entire stream; they overlap the first-token boundary and must
not be summed as an exact decomposition of client TTFT. Retrieval embeds the
query, checks current permissions, queries OpenSearch and rehydrates permitted
Postgres passages. Warm query embeddings hit the existing tenant-scoped cache.
Preparation took 37–66ms. Removing all measured warm retrieval time could not
recover the approximately 0.8s median gap.

## Protocol findings and scope

The observed two model calls are the existing ADR-0016 agentic protocol:
tool selection produces a governed retrieval call; synthesis consumes its
permissioned evidence. No extra model call was observed. Replacing the first
call with unconditional retrieval or a classifier would change behavior and
needs a separately specified decision; it is not an equivalent optimization.
The pinned LiteLLM OpenRouter adapter already caches its AsyncOpenAI client;
connection reuse must be measured rather than assumed missing.

The current gateway grants explicit cache directives only to known OpenRouter
OpenAI/Anthropic families. The configured Ling family receives neither. The
accepted ADR-0016 session-affinity amendment is tracked separately by #763;
implementing it is not evidence that it recovers this latency gap. Each harness
sample creates a new conversation, so affinity could stabilize the two calls
within that conversation but cannot establish cross-conversation cache warmth.

No timing, quality, permission or audit assertion may be weakened to close #802.
A provider/model choice, different retrieval protocol or budget change requires
an explicit decision before implementation. Retain this failing baseline even
if a later independent run passes; provider variance is not a controlled repair.

## HTTP timing diagnostic (same source, no repair)

A second quiet, fresh-process run retained all five original grounded/cited
cases and failed the same budget: **median 2.382s**. Its timing-only plugin
SHA256 was
`ff6cdfd47e904a7f0bcf334b240f0310766323bb998422dced82bda2d6452a08`.
The [network artifact](../../qa/reproductions/profiles/802-network.json) includes
all samples and per-turn SDK reuse/attempt counts, TCP+TLS durations and
request-body-complete to response-header timing. A trace callback recorded
whitelisted event names and timestamps only; no headers, bodies or connection
identifiers were recorded or overridden.

| Sample | Process | Client TTFT | Model turn 1 | Model turn 2 |
|---|---|---:|---:|---:|
| 1 | cold | 3.271s | 1.489s | 1.138s |
| 2 | warm | 2.382s | 1.162s | 1.115s |
| 3 | warm | 2.434s | 1.233s | 1.186s |
| 4 | warm | 2.205s | 1.031s | 1.135s |
| 5 | warm | 2.348s | 1.051s | 1.160s |

Every chat SDK-client resolution reused an existing object. The cold first turn
made two HTTP attempts; the first did not complete response headers. Its raw
error was not captured by the timing plugin, so the cause is not inferred.
Warm turns each opened TCP+TLS (63–76ms), but request-to-response-header wait
was approximately 0.69–0.89s. Adapter work before send was 13–23ms. Even removing
both measured handshakes could not recover the 0.382s median gap. Differences
between this and the first baseline are uncontrolled provider/network variance,
not an improvement caused by instrumentation.

### Bounded local keepalive proof

`qa/reproductions/openai_sse_keepalive.py` runs against its own synthetic
loopback HTTP/1 server and never reads provider settings or calls a provider.
It serves legal chunked SSE whose HTTP tail arrives 30ms after `[DONE]`.
With the same client reused for two requests, pinned OpenAI 2.43.0 used two TCP
connections; a fully consumed HTTPX control used one. Both clients and the
server are closed before exit. Run it with the backend's pinned environment:

```powershell
backend/.venv/Scripts/python.exe qa/reproductions/openai_sse_keepalive.py
```

This proves that stopping at the SSE terminal can lose HTTP keepalive when a
transport tail remains unread. It does not prove the cause of connection churn
on the real provider, nor justify draining private SDK internals in production.
Cancellation/error paths must release upstream resources promptly; a drain
workaround could change those semantics and still cannot close the measured gap.
No SDK-drain workaround is included.

## Decision options

1. Preserve the current Ling route and two-call agentic protocol. The two
   complete five-sample runs above remain failing evidence; there is no proven
   local repair that meets the target, and no green claim or budget waiver.
2. Complete the separately specified #763 cache-affinity work and measure its
   effect independently. It is an existing approved mechanism, but no measured
   latency improvement is assumed here.
3. Compare the shipped FAST route `openrouter/anthropic/claude-haiku-4.5` under
   #490's existing evaluation policy. An explicitly labelled child-process
   benchmark may add it as the ephemeral default registry entry while preserving
   the key/provider, embeddings and original five quality/timing assertions.
   This does not edit `.env`, the application's registry, existing sessions or
   the Ling baseline, and does not repair Ling. A real default change requires
   the RAG bank accuracy evidence mandated by #490 and an explicit decision.
4. Changing the retrieval protocol, choosing another production model/provider,
   or changing the target requires a separate documented product decision.
   None is silently introduced by this investigation.

## Labelled Haiku comparison: incomplete validation, no recommendation

An authorized comparison used the shipped Haiku4.5 FAST entry as an ephemeral
child-process registry default, keeping the same provider key, embeddings,
settings and original five-sample assertions. Source remained `19dec232`.
No `.env`, application registry or existing session was changed. Instrumentation
SHA256 was
`e33c39eaccd1371122ca0ec3e4506625e8ff5aaf660512860b439b729d3402bc`.
The [complete comparison artifact](../../qa/reproductions/profiles/802-haiku-comparison.json)
retains every observed sample and its failure context.

| Sample | Process | Observed first delta | Model turn 1 | Model turn 2 |
|---|---|---:|---:|---:|
| 1 | cold | 2.001s | 2.851s | 1.016s |
| 2 | warm | 3.023s | 1.862s | 1.092s |
| 3 | warm | 3.159s | 2.078s | 1.073s |
| 4 | warm | 3.226s | 2.008s | 1.089s |
| 5 | warm | 1.242s | 1.953s | 1.124s |

Each sample returned through the original exactly-one retrieval/nonempty
passages/citation gate. However, strict teardown failed with the known
`PytestUnraisableExceptionWarning`/`ResourceWarning` before the harness's
post-context terminal, non-retraction and timing checks. Its observed first-delta
median (3.023s) is therefore **not a certified grounded-answer TTFT**: a first
speculative delta could later be retracted. No validation was bypassed or warning
suppressed to obtain a number. This run provides no evidence to recommend a
production model change and is not a fix for Ling. Integrated #801/#803 live
verification remains necessary. No further provider runs were made.

## Repeating the profiling safely

The portable process-only plugin is
`qa/reproductions/chat_latency_profile.py`. Its implementation preserves the
external diagnostic hooks above while replacing the original private absolute
output path with an explicit option. Its source hash therefore differs from the
recorded original instrumentation hashes. It is inactive unless an output path
is explicitly supplied. Configure disposable stores and private provider settings
through the existing Settings/environment path, then from `backend/`:

```powershell
$env:PYTHONPATH = '../qa/reproductions'
$env:RUN_LIVE = '1'
$env:LATENCY_ASSERT = '1'
$env:LATENCY_SAMPLES = '5'
python -m pytest tests/eval/test_chat_latency_live.py -k time_to_first -n 0 -o addopts= -q -s -p chat_latency_profile --latency-profile-output <private-output.json>
```

Keep the default 2s/30s budgets and use a fresh process. The plugin records
whitelisted transport events, SDK reuse booleans and stage timings only; its
output is diagnostic and does not replace any original harness assertion.
Do not publish the harness's raw provider diagnostics or run concurrent suites
while collecting timing evidence. The pure ASGI client and real Redis remain
the measurement boundary; no browser-render or public-internet RTT claim is made.
