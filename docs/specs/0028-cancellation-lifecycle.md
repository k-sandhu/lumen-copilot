# Spec 0028 — Answer cancellation lifecycle and shared budgets (#574)

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
sandbox cancellation where needed, awaited rollback compensation for uploaded
artifacts, and committed the cancelled partial and audit.
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
generation, and retries only that execution's owner-scoped cleanup. Its sandbox
admission lease and daily runtime reservation remain held until cleanup is
confirmed; settlement then occurs once against the original reservation day. One
ownership object holds that lease, its renewal heartbeat, and the reservation from
admission until exactly one of two exits: confirmed termination (or normal
completion), which settles and releases once and only then stops the heartbeat; or
genuine producer death, which stops the heartbeat with no refund so lease expiry
recovers the slot. Nothing else stops the heartbeat: a producer cancellation — user
Stop, the answer deadline, or shutdown — landing at any await of the cleanup
lifecycle (the in-flight call, a kill attempt, the retry backoff wait, the
failure audit/commit, or the deferred finalizer) while the kill is still
unconfirmed hands that same ownership object to whoever continues cleanup, so
admission is never dropped early on a live path. Each attempt
uses a fresh transaction, released before the retry delay, which backs off exponentially
from one second to a sixty-second ceiling; only the first failed attempt writes an error
audit — one error row per execution, latched on the ownership object and shared by
the sandbox service loop and the chat confirmed-kill callback — and every attempt
is logged. A sandbox accounting loss is not a user Stop: it kills
only the in-flight execution, requires the same confirmed kill, and then records the run
killed with reason `sandbox_accounting_unavailable` and returns an outcome — it never
produces a cancelled partial, an allowed cancellation audit, or a cancelled terminal.
Terminal classification is deterministic: the first established cause wins the code
run's `termination_reason`. An accounting loss latched before a Stop stays
`sandbox_accounting_unavailable` (with a `code_run.finished` error audit and never
an allowed `code_run.cancelled`) even when the Stop is what interrupts cleanup,
while the chat message and turn still reflect the user's Stop — the partial answer
kept, marked cancelled. A Stop observed before any accounting failure stays
`cancelled` even if a later renewal tick fails. "First" is decided when the producer
first observes the cancellation: if a renewal failure and a Stop land in the same
event-loop turn, before the producer resumes, the pending Stop wins the tie and the run
is recorded `cancelled`. The producer
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
`finish_reason="cancelled"` (reversible migration `0053_message_finish_reason`).
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

One immutable `AnswerBudgets` value is built from settings in the shared
`chat_composition.build_chat_runtime` builder for live chat, assistant Test, and
headless runs. All three callers retain their runtime-factory injection seams.
Tenant `max_tool_turns` remains the tool-loop override.

| Bound | Setting and default | Enforcement |
|---|---|---|
| Tool turns | `CHAT_MAX_TOOL_TURNS=20`, tenant override wins | At exhaustion, one tool-free synthesis may run within remaining model and wall-clock budgets. |
| Model calls | `CHAT_MAX_MODEL_CALLS_PER_ANSWER=23` | Every provider attempt counts, including retries, fallback attempts, synthesis, length continuation, and optional follow-up suggestions. |
| External web/MCP calls | `CHAT_MAX_EXTERNAL_CALLS_PER_ANSWER=30` | A tool batch that would exceed the remaining allowance is not dispatched; bounded synthesis uses evidence already gathered. |
| Whole-answer wall clock | `CHAT_ANSWER_WALL_CLOCK_SECONDS=600` | An absolute deadline covers preparation and in-flight provider/tool work; exhaustion does not grant another synthesis turn. |
| Per-model-turn deadline | `LLM_INTERACTIVE_TIMEOUT_SECONDS=25` | One resilient turn includes its retries, backoffs, and fallback attempts. |
| Same-route attempts per turn | `LLM_INTERACTIVE_MAX_ATTEMPTS=2` | The shared shortened retry ladder applies to all three callers. |
| Answer output tokens | `CHAT_ANSWER_MAX_TOKENS=1536` | Existing answer/forced-synthesis token ceiling and one bounded length continuation. |

Hard model-call or wall-clock exhaustion produces a 503 terminal problem with
`code=answer_budget_exhausted`. Optional suggestions remain best effort and never
replace an already-published terminal. Per-tenant web/MCP rate limits and per-call
timeouts still apply underneath these answer bounds. Sandbox runner wall-clock
policy remains owned by its separate lane; this change invokes its existing cancel
operation.

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
pending/confirmed/failed Stop and preview abort. Integration regressions cover
shared settings in all three composition modes, local/remote Stop blocked on
artifact compensation, and retained sandbox accounting during failed/interrupted
kill attempts.
Live deployment verification and any environment limits are recorded in the PR
gates table; this specification alone is not evidence that a live gate ran.
