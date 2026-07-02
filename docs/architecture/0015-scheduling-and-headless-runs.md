# 15. Scheduling & headless agent runs

- **Status:** Accepted *(records a sponsor-delegated decision, 2026-07-02; the mechanism choices below are final — the spike is closed)*
- **Date:** 2026-07-02
- **Builds on:** [ADR-0003](0003-application-stack.md) (Celery + Redis — reuse, don't add a broker), [ADR-0004](0004-architecture-boundaries-and-adapters.md) (module boundaries; `tasks/` owns background jobs, `realtime/` owns the WS backplane), [ADR-0008](0008-conflict-free-parallel-delivery.md) (serialized seam → parallel build), [spec 0004](../specs/0004-security-and-domain-invariants.md) (INV-1 tenancy, INV-2 permission, INV-3 citations, INV-6 audit, INV-7 read-before-write)
- **Spike:** closes [#206](https://github.com/k-sandhu/lumen-copilot/issues/206) (SPIKE-5) under epic [#201](https://github.com/k-sandhu/lumen-copilot/issues/201). Deliverable is **this ADR only** — no product code, no migrations.

## Context

Epic [#201](https://github.com/k-sandhu/lumen-copilot/issues/201) (roadmap E7) lets a user **run a saved assistant on a schedule** — a recurring report/update — without typing a prompt each time. Two capabilities are missing and each needs a decision the dependent features can build against:

1. **A dynamic, per-tenant scheduler.** Celery's static `beat_schedule` is fixed at boot; it cannot express *user-created* schedules (a member adds "run my weekly-summary assistant every Monday 08:00 in `America/New_York`") that appear, change, pause, and disappear at runtime, per tenant. We need a Beat that reads its schedule from the database, not from a Python constant, and computes the next fire **timezone- and DST-correct**.
2. **A headless run runtime.** The answer path today — `services/chat_runtime.py` — assumes a **live WebSocket consumer**: its agentic loop publishes every `delta` / tool event / citation to the `realtime/` Redis backplane, and a decoupled WS endpoint relays them to the connected client ([backplane.py](../../backend/app/realtime/backplane.py)). A scheduled run has **no client attached at fire time**. The loop must be able to run with the output **persisted to a durable run transcript** instead of requiring a socket — while still letting a user **optionally attach live** to a run in progress.

Both were explicitly deferred to this spike by epic #201 ("Architecture decided in SPIKE-5"). The constraints in [#206](https://github.com/k-sandhu/lumen-copilot/issues/206) are **already decided and honored here**: reuse Celery/Redis; bind tenant context on the task path via `tenant_session_scope`; enforce per-user permission at retrieval time as the run's *runner* (INV-2 is not relaxed for headless runs); audit every run; no T2+ external write in v1.

## Decision

### 1. Scheduler mechanism — **celery-redbeat** (Redis-backed dynamic Beat)

Adopt **[celery-redbeat](https://github.com/sibson/redbeat)** as the Beat scheduler, reading a per-tenant **`schedules`** table. RedBeat stores each live schedule entry in **Redis** (the broker we already run, [ADR-0003](0003-application-stack.md)) and lets entries be **added / changed / removed at runtime** — exactly the dynamic, user-created shape a static `beat_schedule` cannot express. **No new infrastructure**: it reuses the existing Redis, so the local `docker compose up` floor ([ADR-0005](0005-local-run-and-developer-workflow.md)) is unchanged (one long-running `beat` process is added to the stack; see §7).

| Option | Dynamic schedules | New infra | Timezone/DST | Fit |
|---|---|---|---|---|
| **celery-redbeat** *(chosen)* | Yes — entries live in Redis, mutated at runtime | **None** (reuses the Redis broker) | Per-entry tz on the crontab; DST-correct next-fire | Beat already implied by #236's title ("schedules table + Beat"); smallest delta on the landed stack |
| Static `beat_schedule` | **No** — fixed at boot | None | n/a | Rejected — cannot express user-created, per-tenant cadences |
| celery-beat + custom DB scheduler | Yes | None | Must be implemented | Viable **fallback** (see below) — more code to own than RedBeat |
| APScheduler in a dedicated process | Yes | None (new process) | Yes | Rejected — a **second** scheduling runtime beside Celery; duplicates the worker/broker we already have |

**`schedules` is the source of truth; Redis holds the derived live entry.** A `schedules` row (enabled, with a cron/cadence + timezone) projects into one RedBeat entry keyed by the schedule id; toggling `enabled`, editing the cadence, or deleting the row reconciles the RedBeat entry. Postgres is authoritative (survives a Redis flush); a small reconcile-on-boot / on-write step re-derives entries from the table so a lost Redis entry is rebuilt (mirrors the OpenSearch "Postgres authoritative, derived store reconciled" stance in [ADR-0010](0010-dedicated-text-search-engine.md)).

**`next_run_at` is computed timezone- and DST-correct.** The stored `timezone` (IANA, e.g. `America/New_York`) is applied to the cron/cadence so "08:00 local" lands at the right UTC instant across a DST transition — never "08:00 UTC". Celery's own clock stays UTC (`enable_utc=True`, already set in [celery_app.py](../../backend/app/tasks/celery_app.py)); the tz lives on the schedule entry.

**Fallback (recorded, not chosen):** a **custom DB-backed Beat scheduler** — a `celery.beat.Scheduler` subclass whose `schedule` property reads the `schedules` table directly, no Redis entry store. It is the drop-in if RedBeat proves unsuitable (e.g. a version/maintenance concern): same table, same task, only the scheduler class swaps. This keeps the decision **reversible at the seam** — the `schedules` contract and the `run_assistant` task do not depend on which Beat backs them.

### 2. Data model — `schedules` + `runs` + `run_steps`

Three tenant-scoped tables. Every row carries a non-null `tenant_id` (INV-1); the same fail-closed RLS policy as the existing tables ([spec 0004](../specs/0004-security-and-domain-invariants.md) §2.1) applies. Shapes are **guidance for #235/#236** (the migration issues refine them), consistent with the model sketch in [#206](https://github.com/k-sandhu/lumen-copilot/issues/206).

**`schedules`** — a user-defined recurring run (tenant/owner-scoped):

| Column | Notes |
|---|---|
| `id` (uuid, pk) | |
| `tenant_id` (uuid, fk → tenants, **not null**, indexed) | INV-1 |
| `owner_id` (uuid, fk → users, **not null**) | the runner; retrieval runs **as** this principal (§4, INV-2) |
| `assistant_id` (uuid, fk → assistants) | the saved assistant to run (E1/[#211]) |
| `cadence` (jsonb) | cron expression **or** a structured cadence (`{ every: "day", at: "08:00" }`); one normalized form |
| `timezone` (text) | IANA tz name; drives DST-correct `next_run_at` |
| `input_params` (jsonb) | the assistant's inputs for each fire (the "prompt template" values) |
| `delivery` (jsonb) | where the result lands — v1: `{ "inbox": true, "digest": "daily" }` (§6) |
| `overlap_policy` (text) | `skip` (default) \| `queue` \| `allow` (§5) |
| `enabled` (bool, default true) | pause = set false; projects the RedBeat entry on/off |
| `next_run_at` (timestamptz, null) | computed tz/DST-correct; the upcoming fire shown in the UI |
| `last_run_at` (timestamptz, null) · `last_status` (text, null) | last-fire summary for the list view |
| `created_at` / `updated_at` (timestamptz) | |

**`runs`** — one execution of an assistant (scheduled **or** manual):

| Column | Notes |
|---|---|
| `id` (uuid, pk) | the run's `stream_id` for live attach (§3) |
| `tenant_id` (uuid, **not null**, indexed) | INV-1 |
| `assistant_id` (uuid, fk → assistants) | |
| `assistant_version_id` (uuid, fk) | the **pinned** version executed — a run is reproducible even after the assistant is edited (E1 versioning) |
| `schedule_id` (uuid, fk → schedules, **null**) | null ⇒ a manual/run-now run |
| `trigger` (text) | `schedule` \| `manual` |
| `status` (text) | `queued` \| `running` \| `succeeded` \| `failed` \| `escalated` — the run state machine (illegal transition → 409, INV-8) |
| `inputs` (jsonb) | the resolved `input_params` for this fire (snapshotted) |
| `message_id` (uuid, null) | the assistant message the transcript produced (reuses the chat message/citation tables) |
| `started_at` / `finished_at` (timestamptz, null) | |
| `summary` (text, null) | short digest line for the inbox (§6) |
| `error` (jsonb, null) | typed failure/escalation reason (never a raw vendor string, mirroring the runtime's opaque-error rule) |
| `created_at` (timestamptz) | |

**`run_steps`** — the ordered, persisted transcript of a run (the durable analogue of the WS envelope stream, §3):

| Column | Notes |
|---|---|
| `id` (uuid, pk) | |
| `tenant_id` (uuid, **not null**, indexed) | INV-1 |
| `run_id` (uuid, fk → runs, **not null**, indexed) | |
| `seq` (int, **not null**) | monotonic per run — same ordering guarantee as the WS `seq`; `(run_id, seq)` unique |
| `kind` (text) | `delta` \| `tool_call` \| `tool_result` \| `citation` \| `error` — mirrors the envelope `type`/`name` set |
| `payload` (jsonb) | the envelope `data` (tool args/results, delta text, citation fields) |
| `created_at` (timestamptz) | |

**Citations reuse the existing chain, not a parallel one.** A headless run persists its assistant message via the same `messages` + `citations` tables the chat runtime already writes ([spec 0004](../specs/0004-security-and-domain-invariants.md) §4), so INV-3 (a citation points only to a *permitted, retrieved* passage) holds **identically** for a scheduled run — the guarantee is structural in the shared runtime (§3), not re-implemented. `run_steps` carries the *streaming* transcript (what a live watcher would have seen); `runs.message_id` links the run to its grounded, cited answer.

### 3. Headless run runtime — a persisting sink behind the same producer seam

Refactor `services/chat_runtime.py` so its agentic loop is **decoupled from the WS backplane** and can drive **any envelope sink**. The loop's shape is unchanged (INV-3 grounding, the bounded tool-turn budget, the exactly-one-terminal lifecycle); only *where the envelopes go* becomes pluggable.

- **The seam already exists.** The runtime publishes through the `realtime.Backplane` Protocol ([backplane.py](../../backend/app/realtime/backplane.py)): `publish(stream_id, envelope)`. The chat path passes `RedisBackplane`; the offline tests pass `InMemoryBackplane`. **A headless run passes a new persisting sink** that implements the same `publish` contract by appending each envelope to the run transcript.
- **`RunTranscriptSink`** (new, in `services/` — the runtime's collaborator, not a new boundary owner) writes each published envelope as a `run_steps` row (`seq`, `kind`, `payload`) and folds terminals into `runs.status` / `runs.summary` / `runs.error`. The run then produces a **persisted, cited transcript with no socket present** — the core requirement.
- **Optional live attach (dual-publish when watched).** A user may subscribe to an **in-progress** run over the existing chat WS: the run's `id` is its `stream_id`, so the WS consumer relays it exactly as it relays a chat answer. When a run is being watched, the runtime **dual-publishes** — to the `RunTranscriptSink` (always, durable) **and** to the `RedisBackplane` (so the live watcher sees deltas in real time). A `TeeSink` (fan-out over `[transcript, redis]`) expresses this without the loop knowing whether anyone is watching. Unwatched runs skip the Redis leg entirely (no wasted fan-out); a watcher attaching mid-run drains the transcript-backed replay first, then goes live — the same late-subscriber handling the backplane already implements.
- **Owner/tenant binding is preserved.** Live attach reuses the backplane's `StreamOwner` binding (a run stream is only ever relayed to a principal in the run's tenant who may view it) — INV-1/INV-2 on the *watch* path, unchanged.

**Why this shape:** it is the **smallest correct refactor** — the runtime already speaks to a `publish` Protocol, so headless execution is "a third `Backplane` implementation + a fan-out," not a rewrite of the agentic loop. The INV-3 grounding path (`GroundedCitation.from_passage`, citations drawn only from returned passages) is **untouched**, so a scheduled run cannot fabricate or over-cite any more than an interactive one can.

### 4. Execution path — a Celery `run_assistant(run_id)` task, off the request path

A scheduled fire (Beat) and a **run-now** (API) both **enqueue** a Celery task; neither runs the agentic loop inline.

1. **Fire → enqueue.** RedBeat fires the schedule → a tiny dispatcher creates a `runs` row (`status=queued`, `trigger=schedule`, inputs snapshotted, version pinned) and **enqueues `lumen.run_assistant(run_id)`**. The dispatcher does the *overlap / concurrency* check (§5) **before** enqueuing. Run-now takes the identical path with `trigger=manual` from the `/schedules/{id}/run-now` (or `/runs`) handler — **after-commit**, so the request returns immediately (the pattern `enqueue_ingestion` already uses).
2. **Task runs the headless runtime under tenant context.** `run_assistant` mirrors `ingest`/`sync_source`: a thin sync entrypoint drives an async core via `run_task` (fresh loop + engine dispose, [runner.py](../../backend/app/tasks/runner.py)). The core opens **`tenant_session_scope(tenant_id)`** ([session.py](../../backend/app/db/session.py)) so the whole run executes **as its tenant** with the RLS backstop armed (INV-1, [spec 0004](../specs/0004-security-and-domain-invariants.md) §2.1) — the same binding the request path and chat runtime use. It constructs the `ChatRuntime` with a `RunTranscriptSink` (dual-publishing only if watched) and runs the loop.
3. **Runner is the run's principal (INV-2 is not relaxed).** The runtime's `Principal` is the schedule/run **`owner_id`**, so the `retrieval/` permission filter admits exactly what that user could retrieve interactively — **a headless run can never read what its runner can't**. This is the load-bearing security property of the whole epic: automation does not become an ambient-authority backdoor around the permission model. Negative tests are mandatory (see §9).
4. **Assistant scope is honored.** The run respects the assistant's tool allow-list (CC-A, [#207]) and knowledge scope exactly as an interactive session would — the allow-list gates which tools the loop may call; retrieval stays filtered by the runner's grants.
5. **Audit each run (INV-6).** `run.started` and `run.finished` audit events bracket every run (new actions, additive to the [spec 0004](../specs/0004-security-and-domain-invariants.md) §2.4 taxonomy), plus the per-retrieval `retrieval.query` and `answer.generated` events the runtime already emits. A scheduled run's actor is the **owner** (the run acts on their behalf), tagged as schedule-triggered in metadata — so the trail shows *who* the run ran as and *why it fired*.

### 5. Overlap, concurrency, rate caps, retry

- **Overlap default = `skip` if the prior run is still active** (configurable per schedule via `overlap_policy`). When a schedule fires while its previous run is still `queued`/`running`: **`skip`** records a skipped fire (no new run) — the default, so a slow daily run never stacks up; **`queue`** enqueues behind the active one; **`allow`** runs concurrently. The check is at the **dispatcher** (before enqueue, §4.1), keyed on the schedule's active-run count.
- **Per-tenant concurrency + rate caps.** A tenant cannot flood the worker pool with runs. This **reuses the Redis fixed-window limiter pattern already built for source-sync** ([rate_limit.py](../../backend/app/tasks/rate_limit.py)) — a per-`(tenant, window)` counter admits the first N run-enqueues per window; beyond the cap a fire is **deferred** (re-enqueued with backoff), never dropped. A per-tenant *in-flight* concurrency cap bounds simultaneous runs. Same seam, same Redis, no new infra.
- **Retry/backoff on transient failure; never silently drop.** A transient fault (model/db/storage briefly unavailable) retries with exponential backoff up to a cap, exactly like `ingest`. On exhaustion the run reaches a **permanent, queryable terminal** — `status=failed` with a typed `error` (the dead-letter is a *row*, not a lost message) — and, per §6, surfaces in the owner's inbox. **A run never ends in silence**: every path writes a terminal status, mirroring the chat runtime's exactly-one-terminal contract.

### 6. Controls & delivery (v1)

- **Controls:** `pause` (set `enabled=false` → RedBeat entry removed), `resume` (`enabled=true` → entry re-derived), `run-now` (enqueue an out-of-band `manual` run immediately). Cancelling an in-flight run transitions it to a terminal state (revoke the task + mark the row).
- **Delivery v1 = in-app run inbox + digest.** A completed run lands in an **in-app inbox** (the `runs` list, filtered to the owner) with its `summary` and a link to the full cited transcript; an opt-in **digest** rolls recent runs into one periodic in-app notification. **External channels (email / Slack) are deferred** — they are outbound egress and cross into **T2-ish** territory ([spec 0004](../specs/0004-security-and-domain-invariants.md) §2.5); v1 stays read-before-write with **no external send**. Delivery detail is handed to **[#238]** (F-SCHED-4).
- **Escalation (E7-5) = the `escalated` status.** On ambiguity / missing data / unrecoverable tool failure, the run ends `status=escalated` (a first-class terminal, distinct from `failed`) with the reason in `error`, **notifies the owner** (via the same inbox), and offers **resume / cancel / reroute**. Escalation detail — the triggers and the human-handoff UX — is handed to **[#239]** (F-SCHED-5). This ADR fixes only the *shape*: `escalated` is a run status, not a silent failure.

### 7. Local stack + config

- A long-running **`beat`** service is added to `docker-compose.yml` (`celery -A app.tasks.celery_app beat` with the RedBeat scheduler class), alongside the existing `worker` — it holds no state of its own (schedules live in Postgres/Redis), so it is a thin add. `worker` gains `app.tasks.run_assistant` in its `imports` (mirroring `ingest`/`sync_source` in [celery_app.py](../../backend/app/tasks/celery_app.py)). No new datastore, consistent with [ADR-0005](0005-local-run-and-developer-workflow.md) and the no-`:latest` rule.
- Config via `core/config.py` (`pydantic-settings`): RedBeat key prefix / lock timeout, the per-tenant run concurrency + rate-window caps, retry backoff/cap, digest cadence default. No secrets beyond the existing Redis URL.

### 8. Contract surface (for #234 — frozen first, ADR-0006)

Sketch to hand to **[#234]** (F-SCHED-0), frozen before parallel build per [ADR-0006](0006-contract-first-parallel-implementation.md). REST for CRUD/control (request/response stays REST, [ADR-0003](0003-application-stack.md)); **run streaming reuses the chat WS envelopes** (§3), so no new wire shape for live runs.

- `GET /schedules` — list the caller's schedules (owner-scoped): cadence, timezone, `enabled`, `next_run_at`, `last_run_at`/`last_status`.
- `POST /schedules` — create (assistant, cadence, timezone, inputs, delivery, overlap policy). Owner-gated **T1** write (reversible internal write, [spec 0004](../specs/0004-security-and-domain-invariants.md) §2.5); audited.
- `GET /schedules/{id}` · `PATCH /schedules/{id}` · `DELETE /schedules/{id}` — read / edit / remove (cascades its runs per retention).
- `POST /schedules/{id}/pause` · `POST /schedules/{id}/resume` — toggle `enabled`.
- `POST /schedules/{id}/run-now` — enqueue a `manual` run immediately; returns the new `run_id`.
- `GET /runs` — the run inbox: list runs (filter by `schedule_id`, `status`), newest first; each carries `status`, `trigger`, `started_at`/`finished_at`, `summary`.
- `GET /runs/{id}` — run detail: the full ordered transcript (`run_steps`) + the grounded citations + typed `error`/escalation reason.
- **Live attach:** connect the existing chat WS with the `run_id` as the `streamId`; the consumer relays the run's envelopes (drains the transcript-backed replay, then live) — reusing the `start` → (`delta` | `event` | `citation`) → `done`/`error` sequence unchanged (§3).

Illegal state transitions (e.g. `run-now` on a disabled schedule, resuming a deleted one) → **409/422** at the API boundary (INV-8); missing/expired token → **401** (INV-4); wrong-role or cross-tenant/non-owned schedule → **404**, existence non-disclosure (INV-1/INV-5).

### 9. Boundary table — no new row required

This ADR introduces **no new external system**, so [AGENTS.md](../../AGENTS.md) §6 needs **no new boundary row** and no human approval for one. Everything lands inside modules that already own their concern:

- Scheduler + `run_assistant` task → **`backend/app/tasks/`** (background jobs — already owns Celery). RedBeat is a Beat *scheduler backend* over the **existing Redis broker**, not a new datastore.
- Live-attach fan-out → **`backend/app/realtime/`** (already owns the WS + Redis backplane).
- `RunTranscriptSink` + the headless entrypoint → **`backend/app/services/`** (orchestration; the runtime's collaborator).
- `schedules` / `runs` / `run_steps` models + repositories → **`backend/app/db/`** (already owns relational data).
- `/schedules` + `/runs` wire → **`contracts/`** (already owns the API/WS contract).

(RedBeat is a new *library*, added under `tasks/` — a dependency, not a boundary. If a future external delivery channel (email/Slack, §6) lands, **that** connector is a new module **and** a new §6 row in the same change, per the epic's deferral — out of scope here.)

## Consequences

- **Unblocks the epic.** With scheduler, schema, headless-runtime plan, overlap/concurrency/timezone policy, controls, delivery-v1, escalation shape, and the contract sketch fixed, the dependent features can proceed: **[#234]** (contract freeze) · **[#235]** (headless run runtime + run records) · **[#236]** (dynamic scheduler) · **[#237]** (schedule/run-history UI) · **[#238]** (delivery) · **[#239]** (failure & escalation). Their `blocked-by: #206` is cleared by this ADR merging.
- **Smallest delta on the landed stack.** No new broker, no new datastore, no second scheduling runtime: RedBeat rides the existing Redis, `run_assistant` rides the existing Celery/worker + `run_task` loop discipline, live attach rides the existing WS backplane, and the transcript reuses the message/citation tables. The one moving part is a thin `beat` process.
- **Security is preserved by construction, not bolted on.** A headless run executes under `tenant_session_scope` (INV-1) with the **runner as principal** (INV-2 — it can read only what the owner can), writes citations through the same grounded path (INV-3), and audits `run.started`/`run.finished` (INV-6). The **main correctness risk** is exactly this: a bug that lets a scheduled run retrieve beyond its runner's grants would breach INV-2 — so the parity negative tests (below) are mandatory, same bar as the SSRF chokepoint in [ADR-0009](0009-connector-framework-and-web-source.md) and the retrieval re-proof in [ADR-0010](0010-dedicated-text-search-engine.md).
- **Read-before-write is held.** v1 delivers **in-app only**; there is no T2+ external send path (INV-7), and the escalation status keeps a human in the loop for ambiguity/failure rather than auto-acting.
- **New failure surfaces to own** (called out for #235/#236/#239): overlap races (two fires enqueuing at once — the dispatcher check must be atomic), a lost RedBeat entry (reconcile-from-Postgres on boot), a run that outlives its worker (visible `running` + retry, never a stuck row), and Redis unavailability for Beat (schedules pause until Redis returns — surfaced, not silently missed).
- **Delivery follows the M2/M3 shape** ([ADR-0008](0008-conflict-free-parallel-delivery.md)): serialized prep (this ADR → the `/schedules`+`/runs` contract → the `schedules`/`runs`/`run_steps` migration) → parallel build (headless runtime + scheduler BE ‖ schedule/run-history FE), each slice its own issue/PR with `Closes #`.

### Negative tests the dependent features must ship (spec 0004 §9)

Non-binding here (this is a spike), but named so #235/#236/#239 carry them test-first:

- **INV-1:** a run in tenant A never reads/writes tenant B rows (the task runs under `tenant_session_scope`); a schedule/run in another tenant → **404**.
- **INV-2 (load-bearing):** a headless run **cannot retrieve or cite** a document its runner could not retrieve interactively — the run's permission filter is the runner's, proven by a run whose runner lacks a grant getting **zero** passages another user would see.
- **INV-3:** a run's persisted answer cites **only** permitted, retrieved passages (inherited from the shared runtime — re-proven on the headless path).
- **INV-6:** a run with no `run.started`/`run.finished` audit event → **fail**.
- **INV-7:** no run path performs a T2+ external write (v1 in-app only) → any attempt **forbidden**.
- **INV-8:** an illegal run/schedule transition (run-now on a paused schedule, double-terminal) → **409/422**; the run state machine rejects it.

## Resolved decisions (sponsor-delegated, 2026-07-02)

1. **Scheduler:** **celery-redbeat** (Redis-backed dynamic Beat) reading a `schedules` table — reuse the Redis broker, no new infra. Custom DB-backed Beat scheduler recorded as the fallback.
2. **Schema:** `schedules` (tenant/owner-scoped) + `runs` + `run_steps`; citations reuse the existing message/citation tables.
3. **Headless runtime:** refactor `chat_runtime.py` to drive a **persisting sink** (`RunTranscriptSink`) behind the existing `Backplane` seam; optional live attach via **dual-publish** to the chat WS.
4. **Execution:** a fire/run-now enqueues `lumen.run_assistant(run_id)`; the task runs under `tenant_session_scope` with the **runner as principal** (INV-2 preserved).
5. **Overlap default:** **skip** if the prior run is still active (configurable); per-tenant concurrency + rate caps (reuse the Redis limiter); retry/backoff, never silent-drop.
6. **Delivery v1:** **in-app run inbox + digest**; external channels deferred (T2-ish). Escalation = `escalated` status (E7-5) with owner notify + resume/cancel/reroute.
7. **Contract:** `/schedules` CRUD + pause/resume/run-now, `/runs` list/detail; run streaming reuses the chat WS envelopes.
8. **Boundary table:** **no new §6 row** — everything lands in modules that already own their concern.
