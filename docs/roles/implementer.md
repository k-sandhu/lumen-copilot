# Role: Implementer

> **Cardinality: many, in parallel.** Each Implementer owns exactly one Ready item end-to-end and hands a PR to the Reviewer. Implementers never merge their own work.

## At a glance
| | |
|---|---|
| **Mandate** | Take one **Ready** issue, build the smallest correct change, open a PR with `Closes #N`. |
| **Owns board transition** | `Ready → In Progress → In Review`. |
| **WIP** | **1 feature + 1 cross-cutting** at a time; a spike is a separate slot. |
| **Isolation** | one branch **and** one git worktree per issue. |
| **Enforced by** | instruction (this contract) — not branch protection. |

## Pointed at an epic or a set? → delegate to the Orchestrator
If your target resolves to **more than one issue** (an epic, a milestone, a label, or "all Ready"), do **not** implement them serially. Invoke the **[Orchestrator](orchestrator.md)** protocol to fan out one Implementer per claimable item (cross-cuttings first, one per `area:*`, in parallel). **A single Implementer owns exactly one issue** — the rest of this contract is about that one issue.

## Read first
1. [`AGENTS.md`](../../AGENTS.md) and the **issue** you're claiming (plus any linked spec/ADR).
2. [`docs/WORK_TRACKING.md`](../WORK_TRACKING.md) (the numbered workflow) and [`docs/specs/0001-open-decisions.md`](../specs/0001-open-decisions.md) (don't default open items).

## Protocol
1. **Claim before editing.** Pick an issue in **Ready**, assign yourself, move it to **In Progress**. Never start on an unclaimed or non-Ready item.
2. **Respect area exclusivity.** Don't claim a second issue sharing an `area:*` with another active Implementer unless the dependency edge is explicit and a reviewer is named (spec 0002 §8).
3. **Branch + worktree per issue:** `feat/<#>-slug` (or `fix/`, `docs/`, `chore/`). Never commit to `main`.
4. **Spec-first / test-first** (binds once OD-5 lands): if behavior is unspecified, write the doc/ADR and confirm before coding; write the failing test first, then the smallest change to green.
5. **Build** the smallest correct change. Keep everything in scope for this one issue; a follow-up you discover becomes its **own** issue.
6. **Living docs in the same change** — update any doc/ADR the change affects ([AGENTS.md §7.6](../../AGENTS.md)).
7. **Conventional Commits**, one logical change each. Push the branch.
8. **Open a PR** whose body contains `Closes #<N>`; move the issue to **In Review**. Check each AC you satisfied and note how you verified it.

## Definition of Done (hand-off ready)
- PR open with `Closes #<N>`; branch pushed; board item in **In Review**.
- Each AC addressed, verification noted in the PR body.
- Affected docs/ADRs updated in the same PR.
- `[~]`/`[s]` status markers (if any) carry date + reason + residual risk.

## Loop mode
Invoked with no specific issue, pick the top-priority **Ready** item whose `blocked-by` are all closed and whose `area:*` no other Implementer is actively working; claim it and proceed. Repeat under the shared [loop guardrails](README.md#loops--parallelism); stop when no claimable Ready item remains.

## May NOT
- **Merge your own PR** — that's the Reviewer's job (convention-enforced, since we're not using branch protection yet).
- Claim more than the WIP allows, or grab a non-Ready / unassigned item.
- Widen scope beyond the issue; default an open decision; edit `AGENTS.md`/`CLAUDE.md`/`.claude/`/`glean-user-stories.md` without approval.

## Handoff
The **[Reviewer](reviewer.md)** picks up the PR from **In Review**. If changes are requested, the issue returns to **In Progress** (same agent, same branch). If blocked, move it to **Blocked** and comment why.
