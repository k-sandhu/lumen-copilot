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

The harness (`backend/tests/eval/test_chat_latency_live.py`) drives the **real**
`ChatRuntime` over the **real** `realtime/` backplane — the exact envelope stream
the WS relay forwards to a browser — and reduces it to client-perspective numbers
with the pure helpers in `backend/tests/eval/latency.py`:

- **AC-1 — time-to-first-answer-token.** For a single-tool grounded answer on the
  FAST-tier default model (`Claude Haiku 4.5`, #490), the wall-clock from the
  `start` envelope to the first `delta` carrying answer text, and the total time
  to `done`. Sampled N times and reported at p50 / p95 / max. **Asserts p50 < 2s.**
- **AC-7 — typed terminal error against an unreachable provider.** Routes the
  answer at a black-hole `api_base` and measures `start` → `error`, asserting a
  *typed* terminal (a Problem `code` + `status`) arrives within the interactive
  budget (**≤ 30s**), not after the old ~182s retry cliff.

**Timing source.** Every envelope carries a server-set `ts` ("Server send time"
per `contracts/websocket-envelopes.schema.json`). The harness measures from
envelope `ts` deltas (`start.ts` → first-answer-`delta`.ts), so the numbers are
independent of when the harness happened to drain the bounded replay — they are
the true producer send times, i.e. exactly what a WS client observes.

**Backplane.** When a real Redis is reachable the harness measures over
`RedisBackplane` (so the #487 pooled-publish path is on the clock). Otherwise it
falls back to `InMemoryBackplane` and the printed report names which was used —
so a number that excludes the Redis pub/sub tax is never mistaken for one that
includes it.

## Prerequisites

Same as the live eval (`scripts/run-live-eval.ps1`): the stack up (ADR-0005), an
OpenRouter key, and reachable Postgres + OpenSearch. Redis is optional (see
above). The pytest **skips cleanly** when the key / Postgres / OpenSearch are
absent, so there is nothing to clean up on a machine without the stack.

```powershell
docker compose up -d           # ADR-0005 — brings up Postgres, OpenSearch, Redis
$env:OPENROUTER_API_KEY = 'sk-or-...'
```

## Run it — one command

```powershell
pwsh -File .\scripts\run-latency-harness.ps1 -ApiKey sk-or-...
```

Or directly (the wrapper just sets env and shells out):

```powershell
cd backend
uv run --extra dev pytest tests/eval/test_chat_latency_live.py -s -v
```

The `-s` is required for the harness's printed report to reach the console.

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
- **backplane** tells you whether the Redis pub/sub tax is included. Prefer a run
  with Redis up for the number of record.
- **total answer p50** is the whole answer's settle time (`start` → `done`);
  follow-up suggestions are POST-terminal (#489) and deliberately excluded from
  the answer time (suggestions are disabled in the harness runtime).
- **AC-7 time-to-error** should sit at ~the interactive budget for a *hung*
  provider (the black-hole base) — that is the ~182s → ≤30s fix (#489). A
  connection-*refused* base would fail faster; either way the terminal must be a
  typed `error` with a `code`.

## What this harness does and does not cover

**Live-verified here:** AC-1 (TTFT p50), AC-7 (typed error within budget), and —
as a by-product of AC-1 — that the streamed answer is non-empty and **not**
retracted on the single-tool path (so TTFT is unambiguously answer-token time).

**Pinned offline (not this harness):**
- AC-2 (O(1) Redis connections per answer) — `backend/tests/test_realtime_backplane_pool.py`.
- AC-3 (bounded markdown parses) — `frontend/src/lib/markdown.streaming.test.tsx`.
- AC-4 (persisted bubbles don't re-render mid-stream) — `frontend/src/features/chat/components/ChatThread.renderHygiene.test.tsx`.
- AC-5 (`done` before suggestions) — `backend/tests/test_chat_runtime.py` (post-terminal suggestions).
- AC-6 (narration never answer text; streamed == persisted byte-for-byte) —
  `backend/tests/test_chat_runtime.py`; the harness's `fold_answer_text` mirrors
  the same folding for a live cross-check.

The measurement math itself (envelope → TTFT/total, retract folding, percentiles)
is unit-tested offline in `backend/tests/eval/test_eval_latency.py`, so the
harness's own correctness is proven, not assumed.
