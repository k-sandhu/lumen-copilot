# backend/AGENTS.md — backend coding contract

> Area contract for `backend/`. Subordinate to the root [`AGENTS.md`](../AGENTS.md); it **elaborates, never contradicts**. Read the root contract, [ADR-0003](../docs/architecture/0003-application-stack.md) (stack) and [ADR-0004](../docs/architecture/0004-architecture-boundaries-and-adapters.md) (boundaries) first. The same **prose ↔ mechanism** rule applies: every standard here earns a lint rule, a type check, or a test as the stack matures.

## Stack (fixed by ADR-0003)
Python **3.12**, **FastAPI** (async) on `uvicorn`, Pydantic **v2** + `pydantic-settings`, **SQLAlchemy 2.0 async** + **Alembic**, **Celery** + Redis, **LiteLLM** gateway, **PostgreSQL 16 + pgvector**, **MinIO** (S3). Tooling: **`uv`**, **`ruff`** (lint+format), **`mypy --strict`**, **`pytest`**. No dependency or image unpinned; no `:latest`.

## Layout & layering
Dependencies point inward only: `api/ → services/ → domain/`, adapters hung off `services/`.

```
backend/
  app/
    main.py            # app factory, router mount, middleware, lifespan
    api/               # routers: validate in → call ONE service → shape out. No business logic, no I/O.
      v1/              # versioned routes
      deps.py          # FastAPI dependencies (current user/tenant, db session, services)
    services/          # use-cases / orchestration. The only layer that composes adapters.
    domain/            # entities, value objects, pure policy. NO I/O, NO framework imports.
    db/                # SQLAlchemy models + repositories + Alembic env. The only SQL.
    llm/               # LiteLLM gateway. The only model caller.
    retrieval/         # hybrid search + rerank. The only pgvector/full-text caller.
    search/            # OpenSearch adapter — the single retrieval store (ADR-0010). The only engine caller.
    storage/           # S3/MinIO adapter. The only object-store caller.
    tasks/             # Celery app + tasks. The only place tasks are defined/enqueued.
    realtime/          # WebSocket handlers + Redis pub/sub backplane.
    auth/              # identity & tenant resolution. The only token validator.
    connectors/<name>/ # one adapter per external source (see "Connectors" below).
    core/              # config (pydantic-settings), logging, errors, observability, deps wiring.
  tests/               # mirrors app/: unit (domain/services) + integration (api/db/live)
  alembic/             # migrations
  pyproject.toml       # uv-managed
  Dockerfile
```

**Boundary rule (ADR-0004):** each adapter is the *only* importer of its system's client, and it returns **domain types, not vendor types** — a `services/` caller of `llm/` sees `ChatMessage`/`Completion`, never a LiteLLM object. A router importing SQLAlchemy, a service importing the LiteLLM client, or `domain/` doing I/O is a review/​smoke failure.

## Async & concurrency
- Endpoints, services, and adapters are `async` end-to-end. **Never** block the event loop — no sync DB drivers, no `requests`, no `time.sleep`. Use `asyncpg`/async SQLAlchemy and `httpx.AsyncClient`. Genuinely CPU-bound or blocking work goes to **Celery**, not a thread hack in the request path.
- Anything slow or burst-y (parsing, chunking, embedding, connector sync, re-index) is a Celery task — **idempotent**, retried with backoff, dead-lettered. The request path stays fast.

## Data & migrations
- **Every** schema change is an Alembic migration, reviewed, reversible, with a tenant-isolation lens (CC-2). No autogenerate-and-pray; check the diff.
- Repositories in `db/` expose intent-named methods returning domain types; no leaking `Session` upward. Parameterized queries only — no string-built SQL.
- Vector columns and their ACL/metadata live in the same row so permission-aware retrieval is a `WHERE` clause (ADR-0003 §3).

## LLM gateway (`llm/`) — quality is the point
- The only model caller. Exposes a small async interface (chat, **stream**, embeddings, tool-calls) over LiteLLM; callers never see LiteLLM types. Model IDs, routing, fallback, and limits are config, never hardcoded.
- **Citation-grounded prompting:** the chat runtime must ground answers in retrieved passages and may answer *"I couldn't find it"* — an unsourced claim is a defect (mission filter #2). Carry passage source + char offsets through for citations (CC-11).
- **Retrieval (`retrieval/`)** is hybrid (pgvector semantic + Postgres full-text, fused) with a cross-encoder **re-rank** before context assembly. Permission filter is applied **inside** retrieval, keyed off `auth/` — there is no unfiltered path (CC-1).
- An **eval harness** (golden Q/A-with-source set; groundedness / citation-correctness / retrieval-recall) lives under `tests/eval/` and runs in CI. Prompts are versioned in-repo and testable.

## Connectors (`connectors/<name>/`)
- **Build one by following [docs/guides/building-a-connector.md](../docs/guides/building-a-connector.md)** — the base protocol (`name`/`validate_config`/`sync`/`health`), the optional capabilities (`oauth_spec` / `fetch_changes` / `map_acl`), the error taxonomy, registration-by-drop-in, the SSRF/egress obligations, and the per-deployment OAuth prerequisites. Decisions: [ADR-0009](../docs/architecture/0009-connector-framework-and-web-source.md) (framework + egress) and [ADR-0019](../docs/architecture/0019-connector-sdk-and-oauth.md) (SDK, OAuth, ACL mirroring).
- **Registration is drop-in** (ADR-0008 §3): `connectors/<name>/__init__.py` exposing `CONNECTOR` is auto-discovered — never edit a shared registry.
- **The execution context is framework-supplied** (ADR-0019 §4): connector code never reads the vault, the DB, or mutable module state — it receives an already-authenticated, egress-guarded client on `ConnectorRun` and a frozen `AclMappingContext`. Pinned structurally by the conformance kit.
- **Every registered connector must pass `tests/test_connector_conformance.py`** (rules in `tests/conformance/`), which is parametrized over the real registry — a new connector ships with its harness or the suite fails. A connector declaring `map_acl` must **also** pass the INV-2 ACL negative-test kit.

## Errors, config, observability
- One error model: domain raises typed errors; an exception handler maps them to the contract's error shape (RFC-9457-style problem+JSON). Never leak stack traces or vendor errors to the client.
- All config via `core/config.py` (`pydantic-settings`); **no** `os.environ` reads elsewhere, **no** secrets in code or compose. Fail fast on missing required config at startup.
- Structured logs (`structlog`) with request/tenant correlation IDs; OpenTelemetry traces; Prometheus metrics. This is **ops** observability — distinct from the product **audit log** (CC-8), which emits through one sink for retrieval/answer/action events.

## Testing (test-first — root §9)
- `domain/` unit-tested with zero mocks (it's pure). `services/` tested with faked adapters. `api/` integration-tested against the contract. Streaming paths tested for the full event lifecycle (start → delta → done/error) incl. cancellation.
- **Negative tests required** (root §9): unauthorized → denied, wrong-tenant → not-found/forbidden, malformed input → 4xx, broken invariant. A bug fix ships with its regression test.
- Tests assert the **`contracts/`** shapes, not hand-rolled duplicates.

## Definition of Done (backend slice — also root §15 and ADR-0006 quality bar)
- Endpoints/streams satisfy the frozen `contracts/` shapes; OpenAPI reflects reality.
- Boundaries respected (no cross-adapter imports); migrations included and reversible; config externalized.
- Tests (incl. negative + regression) green; `ruff`, `mypy --strict` clean.
- Slow work is queued, not in-request; errors mapped; audit/permission/citation chokepoints honored where the feature touches them.
- `docker compose up` still converges (backend healthy).
