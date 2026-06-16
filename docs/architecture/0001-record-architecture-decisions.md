# 1. Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-06-16

## Context
This is an agent-driven repo: sessions start cold with no memory of prior reasoning. Decisions that live only in a contributor's (or agent's) head get silently re-litigated, and the contract (`AGENTS.md`) drifts from *why* things are the way they are.

## Decision
We will record architecturally-significant decisions as ADRs in `docs/architecture/`, in Nygard format (*Context / Decision / Consequences*), sequentially numbered and indexed in the README. Accepted ADRs are immutable; a changed decision is captured by a new ADR that supersedes the old one. The threshold for writing one: the choice is **costly to reverse** *and* **not self-evident from the code**.

## Consequences
- A cold agent can reconstruct *why* from the ADR log instead of guessing or re-deciding.
- The threshold keeps the log signal-rich — routine reversible choices stay out of it.
- The first real architecture decisions (stack, boundaries, local-run path — see [../specs/0001-open-decisions.md](../specs/0001-open-decisions.md)) will each get an ADR **before** code lands, which is what unblocks the stack-dependent scaffolding.
