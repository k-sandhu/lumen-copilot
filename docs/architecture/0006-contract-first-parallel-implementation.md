# 6. Contract-first, parallel front/back implementation

- **Status:** Accepted
- **Date:** 2026-06-17
- **Builds on:** [ADR-0002](0002-multi-harness-agent-roles.md) (roles), [ADR-0004](0004-architecture-boundaries-and-adapters.md) (boundaries)

## Context

A grounded-chat feature almost always spans both tiers — a backend endpoint/stream *and* the UI that consumes it. The default failure mode for an agent (or a person) handed such a feature is to build one side fully, then the other, discovering at the seam that the shapes don't match — or worse, to build both sides against an *assumed* contract and find the mismatch only at integration. Serial work is slow; un-coordinated parallel work is slower, because it ends in rework.

The repo already has the [ADR-0004](0004-architecture-boundaries-and-adapters.md) rule that **the FE/BE boundary is `contracts/` and nothing else**, and the [ADR-0002](0002-multi-harness-agent-roles.md) Orchestrator that can spawn parallel workers in isolated worktrees. This ADR composes them into the required workflow for any cross-tier feature: **freeze the contract first, then build both sides in parallel against it.** The sponsor named this explicitly as the way implementers must work.

## Decision

An Implementer who picks up a cross-tier issue runs three phases. A single-tier issue skips to the relevant build.

### Phase 0 — Contract first (no UI/service code yet)
1. Derive the wire shapes the issue needs and express them in **`contracts/`**: OpenAPI paths/schemas for request-response, JSON-Schema **WebSocket envelopes** for streaming/events. Reuse existing shapes; extend additively where possible.
2. Write the contract to be *complete enough to build both sides blind*: success shapes, **error shapes**, pagination, auth/tenant expectations, and for streams the **full event lifecycle** (start → token/delta → tool/citation events → done → error) with ordering and terminal guarantees.
3. **Get the contract reviewed and freeze it.** A frozen contract is the hand-off artifact. Changing it mid-build means pausing both sub-agents and re-freezing — so it is worth getting right once.

### Phase 1 — Parallel build against the frozen contract
The Implementer spawns **two sub-agents in the same message** (so they run concurrently), each pointed at the frozen contract and its area `AGENTS.md`:

- **Backend sub-agent** — implements the endpoints/streams to satisfy the contract, per `backend/AGENTS.md` and the [ADR-0004](0004-architecture-boundaries-and-adapters.md) boundaries. Tests assert the contract.
- **Frontend sub-agent** — implements the UI against the **generated client** and the WS envelopes, per `frontend/AGENTS.md`. It does **not** wait for the live backend: it builds against the generated types and mocks, so a contract match means an integration match.

Neither sub-agent may change the contract unilaterally. A genuine gap pauses both and returns to Phase 0.

### Phase 2 — Integrate & verify
The Implementer wires the real backend to the real frontend over compose, runs `/verify` (and `/verify-live` for the stream round-trip), and self-audits against the quality bar below **before** opening the PR. Integration should be uneventful — that is the payoff of Phase 0.

### The quality bar — "thinking it through" is part of Done
A feature is not done when the happy path renders. Every Implementer (and sub-agent) self-audits against this before hand-off; the area `AGENTS.md` files carry the tier-specific detail:

- **States, not just the success state:** loading, empty, error, partial, and *streaming-in-progress* are all designed and implemented — never a blank pane or a spinner that never resolves.
- **Rendered, not raw:** model output is rendered through the sanitizing markdown pipeline (code blocks, tables, lists) — never dumped as a raw string or injected as unsanitized HTML.
- **Layout that survives real content:** multi-pane views have **independently scrollable** panes; long content, long code lines, and long sessions don't break the layout or force whole-page scroll.
- **Accessible & responsive:** keyboard-navigable, focus-managed, labelled; usable at narrow widths.
- **Cancellable & resilient:** streams cancel on disconnect/navigation; transient failures retry with backoff; nothing wedges.
- **Scales by construction:** slow work is queued, not run in the request path; no N+1s; no unbounded fetches.
- **No temporary decisions:** no hardcoded values that belong in config, no "TODO: handle later" on a real path, no shape a senior engineer would reject in review.

## Consequences

- Cross-tier features parallelize **without** integration rework — the contract is agreed before either side commits to code, and `contracts/` (per [ADR-0004](0004-architecture-boundaries-and-adapters.md)) is the only thing both sides depend on.
- The contract becomes a durable, reviewable artifact and the natural unit of "is this designed yet?" — pushing the hard thinking to the front, where it is cheap.
- The quality bar is written down, so "neglected the error state / dumped raw markdown / non-scrollable panes" is a **reviewable defect**, not a matter of taste.
- Slight up-front cost (you can't start coding the instant you pick up the issue) — accepted, because it is repaid many times over at integration and in review.
- This is a *process* decision that is costly to un-learn once teams form habits and not self-evident from the tree — hence an ADR. Complements [ADR-0002](0002-multi-harness-agent-roles.md); the role contracts in `docs/roles/` reference it.
