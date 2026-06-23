# 8. Conflict-free parallel delivery — vertical slices, serialized seams, auto-discovery

- **Status:** Accepted
- **Date:** 2026-06-22
- **Extends:** [ADR-0006](0006-contract-first-parallel-implementation.md) (contract-first parallel build), [ADR-0002](0002-multi-harness-agent-roles.md) (agent roles), [ADR-0004](0004-architecture-boundaries-and-adapters.md) (boundaries)

## Context

[ADR-0006](0006-contract-first-parallel-implementation.md) lets the *two tiers* of one feature build in parallel. We now want **many features building in parallel** — a fleet of Implementers, each on its own issue, branch, and worktree, merging to `main` with **no manual conflict resolution**.

The theory is one sentence: **two parallel branches conflict only if they edit the same file.** So maximum parallelism is a *file-ownership* problem, not a scheduling one. Every shared file is either (a) edited **once** in a serialized pre-wave PR and then read-only during the wave, or (b) **structurally removed** so features never touch it.

This repo already has scar tissue that proves where the shared files are. Past waves needed avoidable rebases for exactly these:

| Shared file | Why it conflicts | Seen in |
|---|---|---|
| `backend/app/api/v1/__init__.py` | every new endpoint appends an `include_router(...)` | #59/#46, #57/#19 |
| `backend/app/services/__init__.py` | every new service appends an import + `__all__` entry | #59/#46 |
| `frontend/src/routes/router.tsx` | every new screen appends a route object | (current shape) |
| `backend/alembic/versions/*` | linear `down_revision` chain → two new migrations = two heads | (chain `0001→0004`) |
| `frontend/src/api/<x>.ts` | two agents hand-create the same client module | #49/#50 (both made `api/documents.ts`) |
| `uv.lock` / `pnpm-lock.yaml` | any dep bump rewrites the lockfile | #41/#42; pnpm-lock still uncommitted |
| `contracts/openapi.yaml` | both tiers want to edit the wire | handled by ADR-0006 (freeze once) |

## Decision

Ten principles. Each is backed by a **mechanism** (a check that fails when the rule is violated) — per the repo's *prose ↔ mechanism* law, a rule with nothing that fails when broken is not yet real. Mechanisms marked *(pending CI, OD-7)* are enforced by review until CI lands.

### 1. Vertical slices with disjoint file ownership
Each issue owns one slice and edits **only** its own files: FE `frontend/src/features/<x>/**`, BE `backend/app/api/v1/<x>.py` + `backend/app/services/<x>_service.py` + `backend/app/<area>/<x>/**`. A **wave ownership manifest** ships with every wave — a table mapping `issue → allowed path globs`. If a slice needs to touch a path outside its globs, **stop**: the boundary is wrong, or it's a shared seam that belongs in wave 0 (principle 10).
*Mechanism:* a `smoke` check fails any PR whose diff touches paths outside its issue's declared globs *(pending CI, OD-7)*.

### 2. Contract-first, frozen before fan-out
All wire changes for a wave land in **one** serialized contract PR (wave 0) and are **frozen** for the wave. FE builds against the generated client + mocks; BE builds the endpoint. They never touch the same file and integrate at the wire ([ADR-0006](0006-contract-first-parallel-implementation.md)).
*Mechanism:* `smoke` fails any wave-build PR that modifies `contracts/**`; `openapi-spec-validator` gates the contract PR itself.

### 3. Auto-discovery registration — delete the shared aggregators
The single highest-leverage change. Replace manual aggregation with convention-based discovery so adding a feature touches **only that feature's files**:
- **Backend routers:** `api/v1/__init__.py` *scans* its package for modules exposing a `router` and includes them in a stable (sorted) order — instead of a hand-maintained `include_router` list.
- **Backend services:** drop the central `services/__init__.py` re-export wall; callers import `from app.services.<x>_service import ...` directly. The barrel stops being a write target.
- **Frontend routes & nav:** each feature exports its own `route.tsx` (path + element) and `nav.ts` (label, icon, order); `router.tsx` and the nav assemble these via `import.meta.glob`, instead of a hand-edited array.

After this, the four files that forced past rebases are **append-target-free**.
*Mechanism:* the discovery modules are in the manifest as **"no one edits"**; a `smoke` check asserts every `api/v1/*.py` with a `router` is reachable, and that route/nav manifests resolve.

### 4. One migration owner per wave; single-head invariant
Feature PRs **do not add migrations**. Schema for a wave goes through the wave's **one** migration issue (the role #44 played), merged in wave 0. Alembic's linear chain then never forks.
*Mechanism:* CI fails if `alembic heads` reports **>1 head**. Fallback when two are unavoidable: Alembic **branch labels** + a `merge` migration owned by the Reviewer at merge time — never a hand-edited `down_revision`.

### 5. No dependency bumps in feature PRs
Lockfile churn (`uv.lock`, `pnpm-lock.yaml`) is its own PR. Lockfiles regenerate **deterministically** — resolve any collision by *regenerating*, never by hand-merging lock hunks. (Commit `frontend/pnpm-lock.yaml` first — it is currently missing on `main`, a standing reproducibility gap.)
*Mechanism:* `smoke` fails a feature PR that touches a lockfile or `pyproject.toml`/`package.json` dependency block.

### 6. Independent branches off `main`, not stacks
Prefer mocking a dependency over stacking on its branch (FE already mocks BE via the contract). Stacks are allowed **only** along a true hard edge, and then follow the documented replay protocol: `git rebase --onto origin/main <base-tip-SHA> <branch>`, force-push, merge. (Stacked PRs #55/#56 are why this is written down.)
*Mechanism:* the wave plan's dependency graph marks each issue **off-main** (start now) or **gated** (waits for a wave-0 merge); the Orchestrator only fans out off-main issues.

### 7. Codegen at the FE/BE seam — generated files have exactly one author
`frontend/src/api/` is the **generated** client ([AGENTS.md §6](../../AGENTS.md)). Agents **regenerate** it from the frozen contract; they never hand-edit it and never hand-create a parallel `api/<x>.ts`. Conflicts in generated code are resolved by re-running the generator.
*Mechanism:* a generate-and-diff `smoke` check fails if the committed client differs from a fresh generation off the frozen contract.

### 8. Co-locate tests; no shared append targets
Tests live **inside** the feature slice (`features/<x>/__tests__`, `tests/<area>/test_<x>.py`). No central test file, no shared `conftest.py` edit in a feature PR — those are wave-0 seams if they must change.
*Mechanism:* per-folder discovery (pytest/vitest) already gives this; `conftest.py`/shared-fixture files are in the manifest as wave-0-only.

### 9. Small, single-issue PRs that merge fast (squash)
One `Closes #N` per PR; short branch life minimizes drift; the Reviewer is the sole path to `main` ([ADR-0002](0002-multi-harness-agent-roles.md)). Because slices are disjoint, **rebase-forward after each merge is a no-op** on files — there is nothing to resolve.
*Mechanism:* the repo already squash-merges (`(#N)` suffix); the DoD requires one issue per PR.

### 10. Serialize the genuinely-shared seam into wave 0
Anything that *must* be shared — the contract (2), the schema/migration (4), the design-system primitives, the auto-discovery mechanism itself (3) — is built and merged to `main` in a **wave-0 prep PR set before any fan-out**. The fan-out wave then edits only disjoint feature files, so every feature PR auto-merges clean.
*Mechanism:* the **wave-0 gate** — the Orchestrator does not spawn the parallel wave until the wave-0 issues are merged to `main`.

## The operating shape

```
Wave 0  (serialized, small PRs, one owner each — the only place shared files change)
  ├─ ADR-0007 + ADR-0008                      docs
  ├─ auto-discovery registration refactor      api/v1 + services + FE router/nav   ← deletes the 4 conflict files
  ├─ contract amendment + client regen         contracts/ (frozen after)           ← edited once
  ├─ schema migration for the wave             one Alembic revision                 ← single head
  └─ design-system port                        frontend/src/ui shared kit           ← shared FE infra
                          │  (wave-0 gate: all merged to main)
                          ▼
Wave 1  (max parallel — N disjoint slices, each off main; FE‖BE per surface via ADR-0006)
  Search BE ‖ Search FE   Audit BE ‖ Audit FE   Admin BE ‖ Admin FE   Assistant+Docs polish (FE)
                          │
                          ▼
Wave 2  (gated)
  Sources screen + connector framework (#20/#27)   ·   write-side governance (OD-4 tiers)   ·   per-screen live smoke
```

Throughput is set by wave 1's width (≈7 concurrent agents here), not by a serial chain — and nothing in wave 1 shares a file, so the Reviewer merges them in any order with zero manual conflict work.

## Consequences

- Parallel feature PRs **auto-merge**; "rebase to resolve `__init__.py` again" stops happening because those files no longer have append targets (principle 3 retires the #59/#46/#57/#19 class of rebase).
- A small, explicit **up-front cost**: wave 0 must finish before the wide fan-out. Accepted — it is the price of clean parallelism, and it is bounded (docs + four mechanical seams).
- The ownership manifest makes "who may touch what" **checkable**, not cultural — and turns an out-of-bounds edit into a failing check rather than a merge surprise.
- Most mechanisms are *(pending CI, OD-7)* and enforced by the Reviewer until CI exists; CI should implement them as the first gates when OD-7 closes.
- This is a process/architecture decision costly to un-learn once habits form — hence an ADR extending [ADR-0006](0006-contract-first-parallel-implementation.md).
