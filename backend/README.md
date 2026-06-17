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
worker. On boot the backend runs `alembic upgrade head` (enables the `vector`
extension) then serves. Verify:

- Liveness: `GET http://localhost:47181/health` → `200 {"status":"ok",...}`
- Readiness: `GET http://localhost:47181/health/ready` → `200 ready` (or `503
  degraded` with a per-dependency breakdown)
- WebSocket heartbeat: connect `ws://localhost:47181/ws/health` → a `start`
  envelope then periodic `delta` pings (proves the WS path + envelope contract).

## Dev inner loop (uv, outside the container)

```bash
cd backend
uv venv && uv pip install -e ".[dev]"   # create env + install runtime + dev deps
uv run ruff check . && uv run ruff format --check .
uv run mypy app
uv run pytest                            # unit + API tests (no live stack needed)
```

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

[ADR-0003]: ../docs/architecture/0003-application-stack.md
[ADR-0004]: ../docs/architecture/0004-architecture-boundaries-and-adapters.md
