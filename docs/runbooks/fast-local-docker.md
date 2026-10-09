# Fast local Docker runtime

Issue #649 implements the static-SPA intent in ADR-0003 and the existing local
Compose/volume conventions in ADR-0005. This is an opt-in local mode; production
hardening remains in #496.

## Acceptance and verification

The API runs two workers without reload; Celery runs two child processes. The
frontend serves a built SPA through Nginx and proxies REST and WebSocket traffic
on the existing port. Application services use baked source without host source
mounts. Datastores, trust networks, configured ports and named volumes retain
the base Compose definitions. Unauthenticated product access remains denied;
unknown API paths never return the SPA's index HTML.

Nginx disables access logging and discards HTTP error logs at HTTP scope, before
server selection, for every request path. Error context can include request and
upstream URLs or referrers containing OAuth codes/state and WebSocket credentials.
Neither logger may be re-enabled in a server or location. This also suppresses
Nginx request-error diagnostics; use health checks and backend diagnostics instead.
The config regressions check both log directives and every nested override without
starting Nginx or a container.

Verified on 2026-09-30 against the existing local `lumen-copilot` stack:

| Measurement | Development mode | Fast mode |
|---|---:|---:|
| Fresh browser document-page load (one navigation each) | 6,934 ms | 333 ms |
| Frontend HTML, five-request median immediately before/after switch | 1,799 ms | 5.1 ms |
| Proxied `/health`, five-request median immediately before/after switch | 397 ms | 6.4 ms |
| Celery resident memory, Docker snapshot | 2.03 GiB | 209 MiB |

These are local samples under changing host load, not production guarantees.
Four resolved-Compose regressions, 18 backend health/auth tests, and 17 frontend
bundle guards passed. The typechecked frontend image built successfully; Nginx
syntax, live HTTP/WS negative checks, authenticated document-page read-back, and
a real configured-model completion passed. All four datastore container IDs and
volume names were identical before/after; the Alembic revision and the inspected
tenant's three documents and one chat session were unchanged. Inspection showed
zero mounts on all four application containers, two API workers, and two Celery
child processes. The broader production gate and ingestion repair remain scoped
to #496 and #346 respectively.

Run the resolved-Compose and parse-only Nginx config regressions with:

```powershell
python -m unittest discover -s tests -p test_fast_docker.py -v
docker compose -f docker-compose.yml -f docker-compose.fast.yml config --quiet
```

For live validation, check `/`, a direct SPA route, a built asset, `/health`,
`/health/ready`, denied `/api/v1/auth/me`, an unknown `/api/` path, the public
`/ws/health` heartbeat, denied unauthenticated `/ws/chat/` access, and an
authenticated browser page. Inspect mounts and process commands; compare repeated
HTTP timings and idle Docker CPU/memory samples with development mode.

## Build and start

Requires Docker Compose 2.24.4 or newer. Keep your existing `.env`; browser-facing
`VITE_*` values are build arguments, while API/worker configuration stays runtime
configuration. No application secrets are injected into Nginx.

From the usual checkout:

```powershell
docker compose -f docker-compose.yml -f docker-compose.fast.yml up -d --build
```

Open `http://localhost:47180` (or the configured `FRONTEND_PORT`). There is no live
source reload in this mode: edits take effect after rebuilding the relevant image.
The backend entrypoint still applies the existing Alembic migrations on boot.
This mode introduces no schema migration or ingestion repair.

When switching an existing stack from a different checkout or worktree, preserve
its exact Compose project name and use its existing configuration. For the
canonical `lumen-copilot` stack, build first, then replace only application services:

```powershell
docker compose -p lumen-copilot -f docker-compose.yml -f docker-compose.fast.yml build backend worker beat frontend
docker compose -p lumen-copilot -f docker-compose.yml -f docker-compose.fast.yml up -d --no-deps backend worker beat frontend
```

Before replacing services, let any active chat or background job finish. These
commands preserve the existing datastore containers and named volumes. Optional
sandbox and web-search services remain on their current networks.

## Return to development mode

From the original checkout and with the same project name:

```powershell
docker compose -p lumen-copilot -f docker-compose.yml up -d --no-deps backend worker beat frontend
```

The development images, source mounts and hot reload return. Do not use `down -v`
when switching modes: that removes your stored database and uploads.

## Limits

This mode removes development-server and watcher overhead; provider latency,
tool-heavy answers and host resource contention can still affect responsiveness.
It does not fix #346: a 2,048-dimensional embedding configuration against a
Postgres `vector(1024)` column still strands ingestion until that separate issue
is repaired. Do not truncate vectors or reset data as a runtime workaround.
