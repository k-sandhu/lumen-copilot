# Lumen Copilot — backend

FastAPI service (Python 3.12, async). This is the **boot skeleton**: it boots,
health is real, the seams are cut. No product features yet. The binding coding
contract is [`AGENTS.md`](AGENTS.md); architecture is [ADR-0003] / [ADR-0004].

## Run (Docker Compose — the supported path)

```bash
# from the repo root
cp .env.example .env
docker compose up --build
```

Brings up Postgres+pgvector, Redis, MinIO, the backend (web), and a Celery
worker. The worker image includes pinned FFmpeg/ffprobe for audio validation and
video-audio extraction. Browser uploads use signed multipart URLs and browser
media playback uses signed Range GETs, so large file bytes do not pass through
FastAPI. On boot the backend runs `alembic upgrade head` (enables the `vector`
extension) then serves. Verify:

- Liveness: `GET http://localhost:47181/health` → `200 {"status":"ok",...}`
- Readiness: `GET http://localhost:47181/health/ready` → `200 ready` (or `503
  degraded` with a per-dependency breakdown)
- WebSocket heartbeat: connect `ws://localhost:47181/ws/health` → a `start`
  envelope then periodic `delta` pings (proves the WS path + envelope contract).

## Dev inner loop (uv, outside the container)

```bash
cd backend
uv sync --locked --extra dev
uv run --extra dev ruff check app tests
uv run --extra dev ruff format --check app tests
uv run --extra dev mypy app
uv run --extra dev pytest -p no:cacheprovider  # offline, 4 workers, --dist loadfile
```

The default uses four workers to bound memory on shared machines. Override with
`PYTEST_ADDOPTS="-n 2"` to use fewer workers, or `-n 0` for single-process debugging.
Run one pytest invocation at a time and finish it before running another heavy
command. Avoid `-n auto` on large shared hosts; more workers multiply memory use.
`loadfile` keeps module fixtures and ordered tests together. Each invocation
gets a unique temporary root and xdist gives every worker its own subdirectory;
on a shared machine pass `--basetemp <unique scratch directory>` explicitly.
SQLite schema pages are copied from a session template into each test's isolated
database. Test-only Argon2 cost requires `ENVIRONMENT=test`; deployed cost stays
unchanged. Live tests remain collected and skipped by their existing opt-ins.
See [test runtime and CI](../docs/guides/backend-test-runtime.md) for the
isolation, hashing, warning-cleanup and validation contracts.

The compose `backend`/`worker` images install deps into the *system* env (`uv
pip install --system`) so the `./backend:/app` bind-mount doesn't shadow them.

## Layout & the one rule

```
app/
  main.py        app factory: middleware, lifespan, error handlers, mounts
  api/           routers (health + v1 seam) + deps.py — no business logic / I/O
  services/      use-cases (composes adapters) — reserved seam
  domain/        pure entities/policy — no I/O, no framework imports
  db/            async SQLAlchemy engine/session + Base — the ONLY SQL
  llm/           LiteLLM gateway — the ONLY model caller
  retrieval/ storage/ realtime/ auth/ connectors/ tasks/   one adapter per system
  core/          config (the ONLY env reader), logging, errors
alembic/         async migrations (0001 enables pgvector)
tests/           unit (errors, envelopes) + API (/health)
```

**Boundary rule (ADR-0004):** each adapter is the *only* importer of its
system's client and returns **domain types, not vendor types**. A router
importing SQLAlchemy, a service importing LiteLLM, or `domain/` doing I/O is a
review/smoke failure. Everything is async end-to-end; slow work goes to Celery.

`ObjectStore.get` checks tenant ownership before creating a provider client.
Missing objects raise `NotFoundError(object_not_found)`; provider connectivity
and service failures during client setup, request, or body consumption raise
`DependencyError(storage_unavailable)` with safe public text. Cancellation
propagates, and body/client context managers retain responsibility for cleanup.
Regression coverage lives in `tests/test_object_store.py` (#783).

## Beat startup

The composed `celery -A app.tasks.celery_app beat --scheduler redbeat.RedBeatScheduler`
finalizes the app before constructing RedBeat. Finalization registers the existing
expired-upload, run-digest and connector-poll jobs and applies RedBeat's configured
prefix and leader-lock timeout. The `beat_init` hook then rebuilds enabled schedules
from PostgreSQL before the first tick. It is deliberately too late in that hook to
configure RedBeat, which has already captured the app settings.

PostgreSQL remains authoritative; projection keys are schedule IDs, so repeated
startup updates the same entries. Disabled/deleted schedules and mismatched tenant
arguments cannot create runs because the dispatcher rechecks the tenant-scoped row
and its enabled state at fire time. Existing overlap/timezone policy is unchanged.
Boot reconciliation logs `schedule.boot_reconciled` only after every projection
succeeds. A database or projection failure logs `schedule.boot_reconcile_failed`
and exits with status 1 before ticking; CRUD projection remains best-effort.
`tests/test_beat_startup.py` exercises the actual Beat lifecycle (#718).

[ADR-0003]: ../docs/architecture/0003-application-stack.md
[ADR-0004]: ../docs/architecture/0004-architecture-boundaries-and-adapters.md

## Fixed-window admission

The sync and async Redis limiters use one `EVAL` per admission to increment the
tenant counter and set expiry on the first hit atomically (#547). Later hits keep
the original expiry. A cancelled or interrupted client cannot leave a gap between
increment and expiry; an already executed attempt can still spend budget even if
the client never receives its result. Tenant keys, configured prefixes, caps and
windows retain their existing semantics. Redis errors remain fail-open;
cancellation propagates. Async pool ownership and sync client lifetime are unchanged.

Compared with the former split calls, the first hit needs one counter-operation
round trip instead of two; later hits still need one. Redis still performs the
increment and conditional expiry internally. This is a command/round-trip change,
not a measured latency or throughput claim. `tests/test_source_sync_rate_limit.py`
covers offline regressions; the opt-in `tests/test_rate_limit_live.py` verifies
concurrent sync/async admission, TTL, cancellation/process exit and cleanup against
real Redis (`RUN_LIVE=1`, `REDIS_URL` pointing to an isolated instance).
