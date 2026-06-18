# 5. Local-run path & developer workflow

- **Status:** Accepted
- **Date:** 2026-06-17
- **Closes:** OD-5 ([../specs/0001-open-decisions.md](../specs/0001-open-decisions.md)) · informs [`AGENTS.md`](../../AGENTS.md) §10
- **Tracking:** [#16](https://github.com/k-sandhu/beacon/issues/16) · builds on [ADR-0003](0003-application-stack.md)

## Context

[ADR-0003](0003-application-stack.md) chose a multi-service stack (Postgres+pgvector, Redis, MinIO, FastAPI web, Celery worker, Vite frontend). The sponsor's constraint: **a working version of the software is always runnable in a uniform environment**, brought up from a clean checkout with one command, and it must **not collide** with the several other Docker Compose stacks already running on the dev machine. `AGENTS.md` §10 named the verify-tier shape but parked the mechanism on OD-5; this ADR sets the local-run contract those gates wrap.

## Decision

### One command, whole system
`docker compose up` from the repo root brings up the entire stack — Postgres+pgvector, Redis, MinIO, backend (web), Celery worker, and frontend — wired together, healthchecked, and seeded enough to be **demonstrably alive** (frontend loads; it calls the backend `/health`; the backend reports each dependency reachable). A clean checkout is never more than one command from running. The compose file is committed from day one and kept green as features land — a service that doesn't boot is a broken build, not a "later" problem.

### Non-colliding, configurable host ports
Every host port is in a **single high, unusual block (`471xx`)** chosen to avoid the common local ranges, and every port is an **env var with a default** (`.env.example` documents them) so a collision is a one-line override, never a code edit. Canonical defaults:

| Service | Container | Host (default) | Env var |
|---|---|---|---|
| Frontend | 5173 | **47180** | `FRONTEND_PORT` |
| Backend (REST + WS) | 8000 | **47181** | `BACKEND_PORT` |
| PostgreSQL | 5432 | **47182** | `POSTGRES_PORT` |
| Redis | 6379 | **47183** | `REDIS_PORT` |
| MinIO (S3 API) | 9000 | **47184** | `MINIO_PORT` |
| MinIO console | 9001 | **47185** | `MINIO_CONSOLE_PORT` |

Container-to-container traffic uses service names on the compose network and is **not** affected by these host mappings.

### Healthchecks and start order
Datastores (Postgres, Redis, MinIO) declare healthchecks; backend and worker `depends_on` them `service_healthy`; frontend depends on backend. So `up` converges deterministically instead of racing. The backend `/health` endpoint reports liveness; `/health/ready` checks each dependency, which is what makes "demonstrably alive" verifiable rather than assumed.

### Images, volumes, secrets
All images version-pinned (no `:latest`). Named volumes persist Postgres/MinIO across restarts; `docker compose down -v` is the clean reset. **No secrets in the compose file or the repo** — config comes from `.env` (git-ignored), with `.env.example` holding safe local-dev defaults only.

### Dev inner loop
For fast iteration without full containerization: backend runs under `uv run uvicorn ... --reload` against the compose-provided datastores; frontend runs `pnpm dev` (Vite HMR) against the backend. The datastores are *always* the compose ones, so "works on my machine" and "works in compose" cannot diverge. Compose mounts source for hot-reload in dev.

### The `/verify` gate shape (mechanism follows when CI lands — OD-7)
`AGENTS.md` §10 tiers map onto this path:
- **Smoke** — structural, dependency-light: files/contract links resolve, no `:latest`, required env vars present in `.env.example`, `docker compose config` parses. Seconds.
- **Unit** — `pytest` (backend) + Vitest (frontend). Seconds.
- **Live** — real HTTP/WS against the running compose stack with round-trip read-back and teardown (`docker compose down -v`). Minutes.

`/verify` runs smoke → unit → `docker compose config`; `/verify-live` adds the live tier. CI (OD-7) will run the identical chain for local/CI parity. This ADR fixes the *path*; the gate scripts land with the `.claude/` harness (OD-6) and CI (OD-7).

## Consequences

- A cold agent or a new contributor goes from `git clone` to a running, clickable app with one command — the strongest possible "spec ↔ mechanism" backstop for "it works."
- The `471xx` block + env-var indirection means the stack coexists with the sponsor's other local compose projects; collisions are a one-line `.env` edit.
- The compose file becomes a **continuously-enforced contract**: every feature must keep `up` green, so integration rot is caught at commit time, not at demo time.
- Running six services locally is heavier than a monolith — accepted as the cost of the [ADR-0003](0003-application-stack.md) scale seams, and mitigated by healthchecks + the one-command contract.
- **Does not decide:** CI itself (OD-7) or the harness gate scripts (OD-6) — only the local path they wrap. Costly-to-reverse-ish and not self-evident; recorded as an ADR alongside [0003](0003-application-stack.md)/[0004](0004-architecture-boundaries-and-adapters.md).
