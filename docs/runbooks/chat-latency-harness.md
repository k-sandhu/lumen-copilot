# Runbook — the chat-latency harness (#486)

> **What this documents.** How to run the client-perspective chat-latency harness
> and read its output, and which of the parent issue's acceptance criteria it
> verifies live vs which are pinned by offline tests. This is the timing harness
> the parent issue's *instrumentation caveat* said AC-1 and AC-7 could not be
> verified without (the repo has no metrics or tracing). Governed by the sub-issue
> chain #487–#495 and [ADR-0016](../architecture/0016-context-engine-and-cache-first-prompting.md)
> §6 (speculative streaming) and the WS lifecycle in
> [spec 0006](../specs/0006-conversational-chat-affordances.md) §3.1.

## What it measures

The harness (`backend/tests/eval/test_chat_latency_live.py`) drives the **real
client path** end to end — an authenticated `POST .../messages` (202 + stream id)
schedules the answer, then an authenticated WebSocket client consumes the app's
real `chat_ws` relay over the process's **real Redis** backplane — and reduces the
received envelope stream to client-perspective numbers with the pure helpers in
`backend/tests/eval/latency.py`. So HTTP scheduling, stream owner binding,
authentication, and the WS relay are all on the clock (they were not in the earlier
direct-subscribe revision — R2-6):

- **AC-1 — time-to-first-answer-token.** For a single-tool grounded answer on the
  FAST-tier default model (`Claude Haiku 4.5`, #490), the wall-clock from the
  `start` envelope to the first `delta` carrying answer text, and the total time
  to `done`. Sampled N times and reported at p50 / p95 / max. **Asserts p50 < 2s.**
- **AC-7 — typed terminal error against an unreachable provider.** Routes the
  answer at a black-hole `api_base` and measures `start` → `error`, asserting a
  *typed* terminal (a Problem `code` + `status`) arrives within the interactive
  budget (**≤ 30s**), not after the old ~182s retry cliff.

**Timing source — client-perspective, not server send time.** AC-1 is the
wall-clock a *browser* waits, so the authenticated WS client stamps
`time.perf_counter()` the instant it drains each envelope off the socket. TTFAT is
`first-answer-delta receipt − start receipt` and total is
`terminal receipt − start receipt`, both from the consumer's side — so the number
**includes** the HTTP schedule, the Redis publish + pub/sub, and the WS relay to
the client. The envelope's server `ts` is retained only as a diagnostic
(`measure_stream`) to attribute how much of the observed latency is generation vs.
transport; it is not what the AC asserts.

**Grounding gate (R2-6).** A direct model answer can look fast while citing
nothing, so before any AC-1 number is allowed to count, each sample must prove it
took the single-tool grounded path: a wrapping retrieval proxy asserts **exactly
one** `search_text` call that returned **at least one** passage, and the captured
stream must carry **at least one** `event:citation`. An ungrounded sample fails
rather than contributing a misleadingly-fast TTFT.

**Backplane — real Redis is REQUIRED.** The harness measures over the app's own
`RedisBackplane` (so the #487 pooled-publish path is on the clock). There is **no**
`InMemoryBackplane` fallback: a run with Redis unreachable **skips loudly** rather
than quietly reporting a number that excludes the Redis pub/sub tax.

**What the number does and does NOT include (no overclaiming).** It **does**
include the authenticated `POST` → 202 scheduling, the stream owner binding, the
answer producer, the Redis publish + pub/sub, the `chat_ws` relay, and WS frame
delivery to an authenticated client. It **does not** include a real browser's
network RTT / TLS (the ASGI WebSocket client is in-process) nor React render. One
transport caveat: the client — like a browser — connects the WS **after** the 202,
so envelopes already buffered at connect time are drained from the #153 replay in a
burst; for a fast answer the inter-token *pacing* therefore reflects replay drain,
not live per-token arrival. TTFAT stays meaningful because a grounded answer spends
seconds in retrieval + generation *after* the socket attaches, so the first answer
`delta` is delivered live; only its `start` anchor is the connect-time replay of
the already-published `start`, which if anything slightly *under*-counts TTFAT
(a later anchor), never inflates it.

## Prerequisites

Same as the live eval (`scripts/run-live-eval.ps1`): the stack up (ADR-0005), an
OpenRouter key, and reachable Postgres + OpenSearch **and Redis** (Redis is now
required — the harness drives the real backplane and never falls back to in-memory,
see above). Because the harness spends real tokens it is **opt-in** — it runs only
when `RUN_LIVE` is **exactly `1`** (issue #94 parity, mirroring the live
backplane/eval tests; any other value — `true`, `2`, `0`, `no`, empty, or unset —
leaves it off, R2-5) **and** the key / datastores are reachable, and otherwise
**skips cleanly** (no socket probed, nothing spent), so there is nothing to clean
up on a machine without the stack. The `run-latency-harness.ps1` wrapper sets
`RUN_LIVE=1` for you.

```powershell
docker compose up -d           # ADR-0005 — brings up Postgres, OpenSearch, Redis
$env:OPENROUTER_API_KEY = 'sk-or-...'
$env:RUN_LIVE = '1'            # explicit live opt-in (the wrapper sets this for you)
```

## Run it — one command

```powershell
pwsh -File .\scripts\run-latency-harness.ps1 -ApiKey sk-or-...
```

Or directly (the wrapper just sets env and shells out):

```powershell
cd backend
$env:RUN_LIVE = '1'            # without this the harness skips (offline-safe)
uv run --extra dev pytest tests/eval/test_chat_latency_live.py -s -v
```

The `-s` is required for the harness's printed report to reach the console. Omit
`RUN_LIVE=1` and the two tests skip cleanly — that is the default offline posture.

### Tuning (all optional, env-overridable)

| Env var | Default | Meaning |
|---|---|---|
| `LATENCY_SAMPLES` | `5` | AC-1 answers sampled for the p50/p95 |
| `LATENCY_TTFT_P50_BUDGET_SECONDS` | `2.0` | AC-1 p50 budget (the assert) |
| `LATENCY_ERROR_BUDGET_SECONDS` | `30.0` | AC-7 time-to-error budget (the assert) |
| `LATENCY_UNREACHABLE_BASE` | `http://10.255.255.1:9` | the black-hole api_base for AC-7 |
| `LATENCY_QUESTION` | a golden tax-guide question | the single-tool grounded prompt |
| `LATENCY_ASSERT` | `1` | `0` = measure-only (print, don't assert the budgets) |

The wrapper exposes the common two: `-Samples <n>` and `-MeasureOnly`.

## Reading the output

```
chat-latency AC-1 (time-to-first-answer-token)
  backplane        : redis@redis://localhost:47184/0
  samples          : 5
  TTFT p50         : 1.240s  (budget < 2.000s -> PASS)
  TTFT p95         : 1.910s
  TTFT max         : 1.980s
  total answer p50 : 3.470s

chat-latency AC-7 (typed terminal error, unreachable provider)
  backplane            : redis@redis://localhost:47184/0
  unreachable api_base : http://10.255.255.1:9
  interactive budget   : 25.0s (LLM_INTERACTIVE_TIMEOUT_SECONDS)
  terminal             : error code=dependency_unavailable status=503
  time-to-error        : 25.010s (budget <= 30.0s)
```

- **TTFT p50** is the headline AC-1 number. `PASS`/`FAIL` is printed inline; the
  test also *fails* when p50 ≥ budget (unless `LATENCY_ASSERT=0`), so a sub-issue
  cannot close on "looks faster".
- **backplane** is always the real `RedisBackplane` (Redis is required — the run
  skips loudly otherwise), so the Redis pub/sub tax is always included.
- **total answer p50** is the whole answer's settle time (`start` → `done`);
  follow-up suggestions are POST-terminal (#489) and deliberately excluded from
  the answer time (suggestions are disabled in the harness runtime).
- **AC-7 time-to-error** should sit at ~the interactive budget for a *hung*
  provider (the black-hole base) — that is the ~182s → ≤30s fix (#489). A
  connection-*refused* base would fail faster; either way the terminal must be a
  typed `error` with a `code`.

## What this harness does and does not cover

**Live-verified here:** AC-1 (TTFT p50) and AC-7 (typed error within budget),
**driven over the real authenticated HTTP send → `chat_ws` WS relay → Redis path**
(not a direct backplane subscribe); and — as a by-product of AC-1 — that each
sample took the single-tool grounded path (exactly one retrieval call returning
passages + at least one streamed `event:citation`) and that the streamed answer is
non-empty and **not** retracted (so TTFT is unambiguously grounded-answer-token
time). See the harness docstring for the precise does/does-not-include list (no
real browser RTT/TLS or React render; the post-202 replay-drain caveat).

**Pinned offline (not this harness):**
- AC-2 (O(1) Redis connections per answer) — `backend/tests/test_realtime_backplane_pool.py`.
- AC-3 (bounded markdown parses) — `frontend/src/lib/markdown.streaming.test.tsx`.
- AC-4 (persisted bubbles don't re-render mid-stream) — `frontend/src/features/chat/components/ChatThread.renderHygiene.test.tsx`.
- AC-5 (`done` before suggestions) — `backend/tests/test_chat_runtime.py` (post-terminal suggestions).
- AC-6 (narration never answer text; streamed == persisted byte-for-byte) —
  `backend/tests/test_chat_runtime.py`; the harness's `fold_answer_text` mirrors
  the same folding for a live cross-check.

The measurement math itself — both the client-perspective reduction
(`measure_client_stream`: receipt-time → TTFAT/total) and the server-`ts`
diagnostic (`measure_stream`), plus retract folding and percentiles — is
unit-tested offline in `backend/tests/eval/test_eval_latency.py`, so the harness's
own correctness is proven, not assumed. The **opt-in gate** (no import-time socket,
`RUN_LIVE` required and **strict** — only the exact string `1` opens it, R2-5, so
`true`/`2`/`no`/empty/unset all stay off — and the `live` marker present) is pinned
offline in `backend/tests/eval/test_chat_latency_gating.py`, so a regression that
spends tokens on an ordinary `pytest` run — or that loosens the gate to a truthy
parse — would fail there.
