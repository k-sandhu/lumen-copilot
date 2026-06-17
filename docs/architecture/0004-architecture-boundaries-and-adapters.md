# 4. Architecture boundaries & adapters

- **Status:** Accepted
- **Date:** 2026-06-17
- **Closes:** OD-3 ([../specs/0001-open-decisions.md](../specs/0001-open-decisions.md)) · fills [`AGENTS.md`](../../AGENTS.md) §6
- **Tracking:** [#15](https://github.com/k-sandhu/lumen-copilot/issues/15) · builds on [ADR-0003](0003-application-stack.md)

## Context

`AGENTS.md` §6 holds the principle since bootstrap — *"provider-/vendor-specific code stays behind one named module; don't reach for a vendor SDK when an HTTP boundary will do"* — but the concrete `directory → single responsibility` table was parked as OD-3 until the stack landed ([ADR-0003](0003-application-stack.md)). It has now landed, so this ADR fills the table.

The reason this is worth an ADR and not just folklore: in an agent-driven repo, many implementers touch the tree in parallel with no shared memory. Without a written "the **only** place that talks to X" rule, the second agent reaches for the Postgres client inside a router, the third calls the model SDK from a service, and within a week every external system is coupled to everywhere. The mission filters make this acute — permission checks, citations, and audit events must each have **one** chokepoint, or they will be enforced inconsistently and the guarantees become unprovable.

## Decision

### Backend layering (dependencies point inward, one direction only)

```
api/  →  services/  →  domain/        (domain depends on nothing)
                ↘  adapters (db, llm, retrieval, storage, tasks, realtime, auth, connectors)
```

- **`api/`** — routers: parse/validate the request, call one service, shape the response. **No** DB, external, or business logic here.
- **`services/`** — use-cases / orchestration. The only layer that composes adapters. Holds *what the app does*.
- **`domain/`** — entities, value objects, pure policy. **No** I/O, no framework imports. Unit-testable with zero mocks.
- **adapters** — each owns exactly one external system (table below). The **only** place that imports that system's client/SDK.

A router that imports SQLAlchemy, a service that imports the LiteLLM client directly, or domain code that does I/O is a boundary violation and fails review (and, once wired, a smoke grep).

### The boundary table — the one module that owns each concern

| External system / concern | The single owning module | Nobody else may… |
|---|---|---|
| **LLM providers** (chat, stream, embeddings, tools) | `backend/app/llm/` (LiteLLM gateway) | import LiteLLM or call a model/embeddings endpoint |
| **Vector + lexical retrieval** | `backend/app/retrieval/` | issue a `pgvector` / full-text search query |
| **Relational database** | `backend/app/db/` (SQLAlchemy models + repositories) | import SQLAlchemy sessions/models or write SQL |
| **Object storage** (uploads, artifacts) | `backend/app/storage/` (S3/MinIO adapter) | construct an S3/MinIO client |
| **Background jobs** | `backend/app/tasks/` (Celery app + tasks) | enqueue or define a Celery task |
| **Identity & tenant** ("who is asking", tenant scope) | `backend/app/auth/` | resolve a user/tenant or validate a token |
| **External source connectors** | `backend/app/connectors/<name>/` | one adapter per source; talk to that source's API |
| **Realtime transport** (WS + Redis backplane) | `backend/app/realtime/` | open a WebSocket or publish/subscribe Redis |
| **Config & secrets** | `backend/app/core/config.py` (`pydantic-settings`) | read `os.environ` directly |
| **The API/WS wire contract** | `contracts/` | define a request/response/event shape ad hoc |
| **Backend access from the UI** | `frontend/src/api/` (generated client + WS client) | `fetch`/open a socket to the backend elsewhere |

### Cross-cutting chokepoints (mission filters become single seams)

The mission filters are enforced at exactly one layer each, so they are *provable*, not scattered:

- **Permissioned by default** — retrieval queries are permission-filtered inside `retrieval/`, keyed off the identity resolved in `auth/`. There is no retrieval path that skips the filter. (CC-1 [#18], CC-2 [#17].)
- **Citation-backed** — passages carry source + offsets from `retrieval/` through to the answer; the chat runtime refuses to emit an unsourced claim. (CC-11 [#26], CC-6 [#24].)
- **Read before write** — any consequential/write action is out of MVP scope and, when it lands, routes through an approval-gated path, never an adapter side-effect.
- **Auditable** — retrieval, answer, and action events emit through one audit sink. (CC-8 [#23].)

### Adapter rules

1. **Adapters expose domain types, not vendor types.** A caller of `llm/` sees `ChatMessage`/`Completion`, never a LiteLLM response object. Swapping the vendor changes the adapter, not its callers.
2. **HTTP boundary over SDK** when an SDK buys nothing — keeps dependencies and lock-in down (`AGENTS.md` §6 principle).
3. **One adapter, one system.** New external system ⇒ new module + a row in this table (and the table is updated in the *same* change — `AGENTS.md` §7.6).
4. **The contract is the FE/BE boundary.** Frontend and backend integrate only through `contracts/`; neither reaches into the other's internals. This is what makes the parallel build in [ADR-0006](0006-contract-first-parallel-implementation.md) safe.

## Consequences

- Each mission guarantee and each vendor dependency has a single chokepoint — enforceable by review now, by smoke-grep once wired (`AGENTS.md` §10).
- Swapping a vendor (LiteLLM → direct provider, pgvector → Qdrant, MinIO → S3) is a localized, low-blast-radius change — the scale seams from [ADR-0003](0003-application-stack.md) are real because callers never see vendor types.
- `domain/` is pure and fast to test, which is what makes test-first (`AGENTS.md` §9) cheap rather than ceremony.
- The table is **living**: it grows with each new adapter and is mirrored verbatim into `AGENTS.md` §6 in this same change. The per-area contracts (`backend/AGENTS.md`, `frontend/AGENTS.md`, `contracts/AGENTS.md`) elaborate but never contradict it.
- Costly to reverse (boundaries, once crossed everywhere, are expensive to re-establish) and not self-evident from the tree — hence an ADR. Complements [ADR-0003](0003-application-stack.md); supersedes nothing.
