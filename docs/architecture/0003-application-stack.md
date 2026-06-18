# 3. Application stack

- **Status:** Accepted
- **Date:** 2026-06-17
- **Closes:** OD-2 ([../specs/0001-open-decisions.md](../specs/0001-open-decisions.md)) · fills [`AGENTS.md`](../../AGENTS.md) §3
- **Tracking:** [#13](https://github.com/k-sandhu/beacon/issues/13)

## Context

[Spec 0003](../specs/0003-product-scope-and-mission.md) closed OD-1: the MVP is a **multi-tenant, grounded chat assistant** over connected sources and uploaded documents, where every answer is **permissioned, cited, and auditable**. No application stack has been chosen yet (OD-2), which blocks `docker-compose.yml`, the boundary table, the verify gates, and every M0/M1 cross-cutting on the board.

The stack must serve the mission filters, not just "run code." Concretely it must let us enforce permissions at retrieval time, attach passage-level provenance to every answer, stream answers to the user with low latency, ingest many document types at scale, and stay swappable at the provider boundary. The sponsor set hard constraints in session: **open-source only** (until a capability is genuinely unavailable in OSS), **LLM-provider-agnostic** (OpenRouter first, swappable), a **clean front/back split**, **FastAPI** on the backend, **WebSocket** streaming, **background queues** for slow work, **Docker Compose from day one**, and **no temporary decisions** — design as a senior engineer would for scale, maintainability, and extensibility.

This ADR records the *stack and its load-bearing rationale* at the altitude that unblocks scaffolding. The detailed internal design of each component lives in its own cross-cutting ADR (model gateway → CC-9 [#25]; chat/agent runtime → CC-6 [#24]; permission enforcement → CC-1 [#18]; tenancy isolation → CC-2 [#17]; ingestion → CC-5 [#21]; storage sandbox → CC-12 [#22]; citations → CC-11 [#26]). Adapter *boundaries* are [ADR-0004](0004-architecture-boundaries-and-adapters.md); the local-run path is [ADR-0005](0005-local-run-and-developer-workflow.md).

## Decision

**Monorepo, cleanly split, one runtime per tier.**

```
backend/     FastAPI service (Python 3.12, async)          — the only backend
frontend/    React + Vite SPA (TypeScript, static)         — no second server
contracts/   OpenAPI + WebSocket envelopes (source of truth for the wire)
```

### 1. Backend — Python 3.12 + FastAPI (async)
FastAPI on `uvicorn`, fully async. Pydantic v2 for all I/O models; `pydantic-settings` for config (env-driven, no hardcoded config, no secrets in code). SQLAlchemy 2.0 (async) + Alembic migrations as the only path to schema change. `httpx` for outbound HTTP. Layered: **thin routers → services (use-cases) → domain → repositories**; routers never touch the DB or external systems directly (see [ADR-0004](0004-architecture-boundaries-and-adapters.md)).

### 2. Frontend — React + Vite SPA (TypeScript, strict)
A static single-page app, built by Vite, served as static assets. This keeps **FastAPI as the single backend** — there is no Node server to operate, and the FE/BE boundary is the HTTP+WS contract, nothing else. State: **TanStack Query** for server state, **Zustand** for local UI state (no global store dumping ground). Routing: React Router. UI: Tailwind + Radix-based components (owned in-repo). Markdown is **always rendered through a sanitizing pipeline** (`react-markdown` + `rehype-sanitize` + syntax highlighting) — never injected as raw HTML or dumped as a raw string.

### 3. Relational store — PostgreSQL 16 + `pgvector`
One datastore for relational data **and** vector embeddings. Vectors live transactionally beside their metadata and ACLs, which is what makes **permission-aware retrieval** a `WHERE` clause rather than a cross-store reconciliation problem. Retrieval is **hybrid** — `pgvector` semantic similarity fused with Postgres full-text (lexical) — behind a retrieval adapter so a dedicated engine (Qdrant/OpenSearch) can replace it when corpus scale demands, without touching callers.

### 4. Cache, broker & realtime backplane — Redis
One Redis serves three roles: cache, **Celery broker/result backend**, and the **pub/sub backplane** that fans streamed LLM tokens to the right WebSocket connection regardless of which backend instance holds it — so the web tier scales horizontally with no sticky sessions.

### 5. Object storage — MinIO (S3-compatible)
Uploaded documents and derived artifacts live in MinIO locally, addressed through an S3-compatible adapter. Production swaps the endpoint for any S3-compatible service; application code does not change. (Retention/sandbox rules → CC-12 [#22].)

### 6. Background work — Celery + Redis
All slow or burst-y work — document parsing, chunking, embedding, connector syncs, re-index — runs on **Celery workers**, never in the request path. Tasks are **idempotent, retried with backoff, and dead-lettered**. The web tier and worker tier scale independently.

### 7. LLM access — LiteLLM gateway, OpenRouter first
All model calls (chat, streaming, embeddings, tool-calling) go through **LiteLLM** as the provider gateway, configured with **OpenRouter** as the first provider. LiteLLM gives provider-agnosticism out of the box; OpenRouter gives breadth of models on day one. This is confined to **one backend module** (`app/llm/`, [ADR-0004](0004-architecture-boundaries-and-adapters.md)) — the only place in the system that speaks to a model — so swapping LiteLLM itself, or pointing it at direct providers / a self-hosted model, is a localized change. Detailed gateway design (routing, fallback, cost/limits, caching) is CC-9 [#25].

### 8. Realtime — WebSocket streaming
Token streaming uses **WebSocket** (FastAPI native) on the request-serving async path for lowest latency, fanned via the Redis backplane (§4). Client disconnect cancels generation; per-tenant rate limits sit at the gateway. Request/response APIs stay REST (auto-emitted OpenAPI); only streaming uses WS.

### 9. Answer quality is a first-class concern, from the start
Because "high-quality answers" and "citation-backed" are load-bearing, the stack commits to: structure-aware **chunking** with overlap and rich metadata (source, ACLs, char offsets for citations); **hybrid retrieval + cross-encoder re-rank** (OSS reranker) before context assembly; **citation-grounded prompting** where an unsourced answer is a defect and "I couldn't find it" is a valid answer; and an **evaluation harness wired in from day one** (golden Q/A-with-source set; groundedness / citation-correctness / retrieval-recall measured in CI). Quality is measured, not asserted. (Mechanics → CC-6 [#24], CC-11 [#26].)

### 10. Tooling
- **Backend:** `uv` (env + deps), `ruff` (lint + format), `mypy --strict`, `pytest`. `structlog` + OpenTelemetry + Prometheus for observability (distinct from the product audit log, CC-8 [#23]).
- **Frontend:** `pnpm`, Vite, TypeScript strict, ESLint + Prettier, Vitest + Testing Library (unit/component), Playwright (E2E).
- **Contract:** OpenAPI is emitted by FastAPI and the TS client is **generated** from it; WebSocket envelopes are shared JSON Schema. No hand-maintained, drift-prone client.
- **Pinning:** every image and dependency is version-pinned; no `:latest` (smoke-checked).

## Consequences

- The board's M0/M1 cross-cuttings unblock: each now builds within a known stack and the [ADR-0004](0004-architecture-boundaries-and-adapters.md) boundary table.
- **OSS-only and LLM-agnostic are structural, not aspirational:** every external dependency above is OSS, and the one proprietary surface (the model provider) sits behind a single swappable module.
- **Scale paths exist by construction** without premature build-out: stateless web tier + Redis backplane (scale out web), Celery workers (scale out compute), retrieval behind an adapter (swap the engine), S3-compatible storage (swap the endpoint). We adopt the simpler option now (pgvector, single Redis) *with the seam already cut*.
- One more service to operate locally than a monolith would need (Postgres, Redis, MinIO, web, worker, frontend) — accepted, and the reason [ADR-0005](0005-local-run-and-developer-workflow.md) makes `docker compose up` the one-command contract.
- **This ADR does not decide:** the identity provider technology (CC-3 [#19] — the *boundary* is reserved, the IdP is its call); detailed retrieval/gateway/citation internals (their CCs); security & domain invariants (OD-4 [#14]); CI (OD-7). Those remain open and must not be defaulted.
- Costly to reverse (datastore, framework, and provider-abstraction choices propagate widely) and not self-evident from an empty tree — hence an ADR. Complements [ADR-0001](0001-record-architecture-decisions.md)/[0002](0002-multi-harness-agent-roles.md); supersedes nothing.
