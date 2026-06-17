# contracts/AGENTS.md — the FE/BE wire contract

> Area contract for `contracts/`. Subordinate to the root [`AGENTS.md`](../AGENTS.md). This directory is **the single source of truth for the wire between frontend and backend** (root §6 / [ADR-0004](../docs/architecture/0004-architecture-boundaries-and-adapters.md)). Cross-tier features are built **contract-first** ([ADR-0006](../docs/architecture/0006-contract-first-parallel-implementation.md)): the shapes here are agreed and frozen *before* either side writes feature code.

## What lives here
- **`openapi.yaml`** — OpenAPI 3.1 for all request/response (REST) endpoints. The hand-authored design source; the backend implements to match and the frontend's client is **generated** from it.
- **`websocket-envelopes.schema.json`** — JSON Schema for the **WebSocket message envelope** (the transport framing for all streaming/event traffic). Concrete event payloads for a feature are added here as that feature's contract.
- Generated artifacts (the TS client, server stubs) are **build outputs**, not committed sources — they regenerate from the files above.

## The envelope convention (WebSocket)
Every WS message is a JSON **envelope**: a `type` discriminator, a `streamId` correlating a logical stream, a monotonically increasing `seq`, and a `data` payload typed per `type`. Every stream has a defined lifecycle with a **terminal** event:

```
start  →  (delta | event)*  →  done | error
```

- `start` opens a stream (carries `streamId`); `delta` carries incremental output (e.g. tokens); `event` carries structured side-band data (e.g. citations, tool calls — defined per feature); `done` / `error` are terminal and exactly one must arrive. Ordering is guaranteed by `seq`. Clients must handle `error` and unexpected disconnect identically (treat as terminal, allow retry).
- This is the **transport** convention; the *product* events that ride it (chat tokens, citation events, tool calls) are defined by their cross-cuttings (CC-6 [#24], CC-11 [#26]) — do not pre-invent them here.

## Rules for changing the contract
1. **Contract before code.** For a cross-tier change, edit the contract here and get it reviewed/frozen **first** (ADR-0006 Phase 0). Only then do the parallel FE/BE sub-agents start.
2. **Additive by default.** Add fields/endpoints/event types; don't repurpose or silently change existing shapes. A breaking change is a versioned change (`/v2`, or a new event `type`) with both sides migrated in the same PR or behind a flag.
3. **Complete enough to build blind.** Specify success shapes, **error shapes**, pagination, auth/tenant expectations, and for streams the **full lifecycle** (events, ordering, terminal guarantees). If a sub-agent has to guess, the contract isn't done.
4. **One error shape.** Errors use a single problem model (RFC-9457-style `application/problem+json`); both tiers depend on it.
5. **No drift.** The backend's emitted OpenAPI must match `openapi.yaml`; a smoke check enforces this once CI lands (OD-7). Until then, reviewers verify it.

## Definition of Done (contract slice)
- Shapes are complete (success + error + lifecycle), additive or properly versioned, and reviewed/frozen before parallel build.
- The error model is reused, not re-invented.
- Backend and the generated frontend client both build against these shapes; `docker compose up` round-trips them.
