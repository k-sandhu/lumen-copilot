# Spec 0028 — Answer cancellation lifecycle and shared budgets (#574, #833)

The wire contract is [OpenAPI](../../contracts/openapi.yaml)
(`POST /chat/streams/{streamId}/cancel`) and the
[WebSocket envelope schema](../../contracts/websocket-envelopes.schema.json)
(`cancelled`). This spec records the lifecycle and cost bounds shared by live
chat, assistant Test, and headless answers. Scheduled-run cancellation is outside
this change.

## Lifecycle and acknowledgement

```text
send (202, stream_id) -> producer registered under stream_id
  -> start -> (delta | event)* -> exactly one of done | error | cancelled
```

`done`, `error`, and `cancelled` are mutually exclusive terminals. The runtime
owns one monotonic sequence counter, a terminal guard, and a stable terminal
sequence for publish retries. A cancelled stream has no suggestions window and
accepts no further answer, citation, tool-result, or artifact events.

An explicit Stop uses the authenticated cancel endpoint. It checks the same
stream-to-owner binding as the WebSocket consumer: missing, expired, same-tenant
non-owner, and cross-tenant stream ids all return 404 (INV-1/INV-2). Missing or
expired authentication returns 401. Denied cancellation attempts use the existing
durable permission-denial audit path.

The endpoint records a durable cancel key and publishes a cancel notification on
the existing backplane. A producer watcher subscribes before checking that key,
so cancellation interrupts an in-flight provider or tool await on any serving
replica and also survives a signal sent before the producer watcher starts.
The local stream registry addresses active tasks by stream id and prunes completed
entries; completion checks across replicas use the terminal retained in replay.

The endpoint returns 200 only after observing the producer's terminal. Its
`terminal` field carries that exact settled envelope from bounded replay. If
socket delivery was lost, the client applies this confirmed outcome in place of
its local disconnect error and reloads history once. An acknowledgement from a
previous stream cannot mutate the current stream; an actual server terminal
already received remains final.

Response status:

| Response status | Meaning |
|---|---|
| `cancelled` | Provider/tool awaits were reaped, the cancelled state and audit committed, and the producer published its cancelled terminal. |
| `already_completed` | The stream had completed, failed, or entered its final commit before Stop could take effect. Stop leaves that outcome intact. |

Repeated Stop is idempotent, including concurrent requests: an already-cancelled
stream returns `cancelled`; a completed stream returns `already_completed`.
The runtime owns finalisation, so repeated endpoint requests cannot create extra
messages, cancellation audit records, or terminal events.

The final answer commit is the completion boundary. Immediately before committing,
the runtime checks cancellation, then enters a completion critical section. A
Stop that loses that race waits for the completion terminal instead of replacing
a durably completed answer with a cancelled partial. A failed commit still follows
the existing error path.

Cancellation acknowledgement is the `cancelled` terminal and the corresponding
200 response. Before publishing it, the runtime has stopped dispatch, closed the
provider generator, cancelled/reaped active tool/HTTP awaits, invoked existing
sandbox cancellation where needed, and committed the cancelled partial and audit.
A cancellation check after awaited I/O rejects late results even when an adapter
returns while cancellation is in progress. After acknowledgement there is no new
provider/tool call or answer/citation/artifact persistence, and no rolling-summary
refresh is enqueued for the cancelled answer.

A cleanup timeout returns 503 after `CHAT_CANCEL_TIMEOUT_SECONDS` (default 30);
it does not claim cancellation succeeded. The signal remains set and a client may
retry Stop while retaining the partial. If the producer died, no terminal can be
confirmed and the same timeout applies. If the cancellation state/audit cannot be
committed, the stream emits an `error` with `cancellation_persist_failed`; the
cancel endpoint returns 503, rather than claiming the partial was saved. Owner
bindings and replay expire after their existing 900-second retention; subsequent
requests whose owner binding expired return 404. This is bounded replay and
cooperative cancellation, not durable recovery of a killed producer.

An unsuccessful or interrupted sandbox kill remains pending cleanup, never an
ordinary failed tool result. The producer retains the same code-run id and sandbox
generation, and retries only that execution's owner-scoped cleanup. Each attempt
uses a fresh transaction, released before the one-second retry delay. The producer
publishes no terminal or cancelled partial/audit until the kill and its durable
state are confirmed. Repeated Stop waits on that same producer; each request can
return 503 while cleanup is pending. Once cleanup succeeds, the producer commits
one cancelled partial/audit and emits one cancelled terminal. Shutdown still
drains the shielded cleanup within its
existing grace; force shutdown can leave cleanup unconfirmed and must not emit
successful cancellation. Producer death recovery remains outside this lifecycle.

## Disconnect and partial answers

A plain WebSocket disconnect drops only its subscription. The producer continues;
a reconnecting subscriber can resume from the existing bounded replay. Reloads,
session switches, and temporary connection loss therefore do not silently cancel
an answer the user intends to reattach to. Explicit Stop cancels backend work.
A process-shutdown task cancellation without a user-cancel request retains the
existing retryable 503 `error` outcome; it is not labelled user cancellation.

The accepted owner decision is: **Stop keeps the partial answer, marked
cancelled.** The runtime persists the partial under the pre-minted message id with
`finish_reason="cancelled"` (reversible migration `0052_message_finish_reason`).
The text snapshot includes any pre-cancellation coalescer tail flushed before the
terminal; output returned after cancellation is observed is discarded. A Stop
before the first answer token persists an empty cancelled turn so history retains
the stopped state. A cancelled partial has no attached citations. The frontend
retains the partial while Stop is pending, labels the acknowledged cancelled turn,
and renders the same status after history reload. It must not claim Stop succeeded
when acknowledgement is unavailable.

To reverse the owner decision, remove partial-message persistence and the nullable
finish-reason column and render a transient stopped state. The task cancellation,
backplane signal, budgets, terminal lifecycle, and cancellation audit remain.

Exactly one `answer.cancelled` audit record accompanies the partial (INV-6),
identifying the tenant, actor, stream id, session id, model, and partial length.
Provider-reported token spend up to cancellation is retained through the existing
failure-path usage salvage and reported on the terminal. Outcome telemetry records
complete, failed, budget-exhausted, and user-cancelled answers with model-call and
usage totals. Plain transport disconnect does not produce a second answer outcome.

Failure settlement also wins once its cleanup begins: a later Stop awaits the
original error terminal. An already-accepted Stop retains its cancelled outcome
if an adapter translates cancellation into a provider error. Both normal answers
and clarifying questions use the same final-commit boundary and completed-usage
snapshot. Redis owner, replay, and cancellation keys live at least as long as the
configured answer deadline plus cancellation and shutdown cleanup margins, so a
longer configured answer remains cancellable with a refreshed access token.


## Shared server budgets

One immutable `AnswerBudgets` value is built from settings at each existing
`ChatRuntime` construction site: live chat, assistant Test, and headless runs.
Tenant `max_tool_turns` remains the tool-loop override.

| Bound | Setting and default | Enforcement |
|---|---|---|
| Tool turns | `CHAT_MAX_TOOL_TURNS=20`, tenant override wins | At exhaustion, one tool-free synthesis may run within remaining model and wall-clock budgets. |
| Model calls | `CHAT_MAX_MODEL_CALLS_PER_ANSWER=23` | Every provider attempt counts, including retries, fallback attempts, synthesis, length continuation, and optional follow-up suggestions. |
| All tool calls | `CHAT_MAX_TOOL_CALLS_PER_ANSWER=60` | Internal and external admissions share one allowance; exhausted answers move directly to synthesis. |
| Calls per round | `CHAT_MAX_CALLS_PER_ROUND=8` | Admit at most this many calls, within the remaining answer and external allowances. Concurrency is a separate bound. |
| External web/MCP calls | `CHAT_MAX_EXTERNAL_CALLS_PER_ANSWER=30` | Admit the portion that fits, preserving internal calls in the same round; at exhaustion move to synthesis. |
| Tool-loop input tokens | `CHAT_MAX_INPUT_TOKENS_PER_ANSWER=500000` | Before each provider attempt, used input plus the fitted request's projected prompt must fit. Uses the context engine's wire-aware token counter, including schemas and tool metadata. |
| Tool-loop output tokens | `CHAT_MAX_OUTPUT_TOKENS_PER_ANSWER=16000` | Each request's `max_tokens` is capped to the remaining allowance; no request starts after it is spent. |
| Tool-loop billed USD | `CHAT_MAX_COST_USD_PER_ANSWER` unset (off) | Before each attempt, stop when the measured subtotal has reached the ceiling or an earlier call is unpriced. Set a finite positive ceiling in production. |
| Whole-answer wall clock | `CHAT_ANSWER_WALL_CLOCK_SECONDS=600` | An absolute deadline covers preparation and in-flight provider/tool work; exhaustion does not grant another synthesis turn. |
| Per-model-turn deadline | `LLM_INTERACTIVE_TIMEOUT_SECONDS=25` | One resilient turn includes its retries, backoffs, and fallback attempts. |
| Same-route attempts per turn | `LLM_INTERACTIVE_MAX_ATTEMPTS=2` | The shared shortened retry ladder applies to all three callers. |
| Answer output tokens | `CHAT_ANSWER_MAX_TOKENS=1536` | Per-request answer/synthesis ceiling. Ordinary length continuation remains inside the tool-loop token/cost ceilings; forced synthesis has no continuation. |

Hard model-call or wall-clock exhaustion produces a 503 terminal problem with
`code=answer_budget_exhausted`. Optional suggestions remain best effort and never
replace an already-published terminal. Per-tenant web/MCP rate limits and per-call
timeouts still apply underneath these answer bounds. Sandbox runner wall-clock
policy remains owned by its separate lane; this change invokes its existing cancel
operation.

## Admission, accounting, and synthesis (#833)

Tool calls are admitted in provider order. A round admits at most
`min(max_calls_per_round, remaining max_tool_calls)` calls, with each external
call additionally requiring an external slot. Unadmitted calls never reach a
handler. Every call still receives one paired tool result: `ok=false`, a stable
budget error, a code-built reason, and explicit instructions saying whether a
retry in the next round is useful. Per-round refusals can be retried while an
answer allowance remains; per-answer refusals cannot. The runner records each
refusal through its ordinary denied invocation and ordered audit path (INV-6).
A skipped call consumes no execution slot, and opens no handler/session scope.
Admitted slots remain charged even if governance denies or reuses a call; this
conservatively bounds dispatch as well as actual execution.

Before **every** tool-loop provider attempt, including retry and failover, the
runtime projects the fitted messages and offered tools using the same model's
context token counter. It reserves input and the requested output allowance
until usage settles. Input retains the larger of projection and known reported
spend, including usage reported by a failed attempt before retry. Missing usage
retains the reservation, so unknown usage cannot buy free requests. Cost-only usage with default zero token fields also
retains the requested output reservation; only a completed positive completion
measurement releases its unused output. These control counters are separate from the honest
provider usage ledger; they never invent billed token measurements. Request
output is bounded by `min(answer_max_tokens, remaining output)`; legacy callers
without a finite answer setting use the configured default of 1536 for this
request bound. Tokenizer projection is conservative local admission, not a
claim that provider tokenizers must agree with it.

Cost uses only ADR-0031's provider-billed subtotal across route scopes. Cache
hits still count their prompt tokens. A measured zero is priced; missing or
failed/abandoned dispatch cost is unpriced. With a cost ceiling enabled, the
first unpriced attempt stops further tool-loop requests under the `cost` policy,
with its unpriced count in usage and outcome telemetry. No local estimate is
substituted. A measured cost can cross its ceiling by at most one tool-loop
request, since the price is only available after that request.

A stop at tool turns, tool calls, external calls, input tokens, output tokens,
or cost allows **one** tool-free synthesis provider attempt outside those
phase ceilings. It uses `answer_max_tokens` (1536 when an old caller supplies no
finite setting) and remains inside the whole-answer model-call and wall-clock
caps. It has no retry, failover, or length continuation. A normal tool-free answer
that is already length-limited retains its partial and disclosure if its
continuation cannot fit; it does not open an additional synthesis request.
The absolute token/cost bound is therefore the tool-loop ceiling (including the single-request cost
overshoot) **plus that one bounded synthesis request**; synthesis cost is billed
and reported even though it cannot be known before dispatch. An already-spent
model-call or wall-clock cap grants no extra request. Optional suggestions use
their existing separate usage scope and global model/time caps.

## Honest limit disclosure (#833)

The nullable `limit` object on `done.data` and the history `Message` DTO contains
`kind` and `message`. Kinds are `tool_turns`, `tool_calls`, `external_calls`,
`input_tokens`, `output_tokens`, and `cost`. One complete sentence is selected
from the backend's central sentence table. It includes no model-authored text
or numeric allowances. The cost sentence covers both measured exhaustion and
an unpriced request that prevents confirming the spend bound. The frontend
renders that sentence as a muted note beneath the answer, live and after reload.

Only the kind persists in the nullable `messages.limit_kind` column (reversible
migration `0055_answer_limit`); reload reconstructs the identical sentence.
Ordinary answers omit the field from `done` for wire compatibility and have a
null/absent history object. Unknown stored kinds produce no disclosure.
Outcome telemetry includes the limit kind and unpriced count without content.
Cancellation retains its separate lifecycle and marker.

Reversal: remove the new settings/admission checks and disclosure projection,
downgrade 0055 (losing stored limit kinds), and remove the note from both UI
paths. Existing cancellation, billed usage, and global model/time caps remain.

## Assistant Test

Test uses the same runtime budgets. The blocking test endpoint watches client
disconnect; TestPanel Stop aborts its request and signals cancellation to the
preview runtime. The server waits for runtime cleanup instead of abandoning a
background preview. Runtime transcript, partial, citation, artifact, and internal
audit writes remain inside the preview's rollback-only transaction. The request
commits one `assistant.tested` event whose outcome is `cancelled`; it does not
persist a chat transcript for the preview.

## Verification

Offline event-driven tests cover remote cancellation during a parked provider,
late tool results, cancellation between retries, repeated Stop, cross-tenant and
non-owner denial, empty/partial persistence, single cancellation audit and terminal,
model-attempt caps, external-call caps, and a deterministically expired answer
deadline. A paused final commit verifies that completion wins a late Stop with
one success terminal and preserved completed-usage telemetry. Frontend tests cover
pending/confirmed/failed Stop and preview abort.
Live deployment verification and any environment limits are recorded in the PR
gates table; this specification alone is not evidence that a live gate ran.

The #833 regression suite additionally covers every limit kind before the
refused request, partial admission and denied-call audit, adversarial wide and
long tool batches, token reservations, retry/failover checks, missing prices,
normal wire compatibility, tenant-scoped disclosure reload, reversible DDL,
and frontend live/reload rendering. The PR records red/green and per-bound
revert checks; this text does not substitute for executed verification.
