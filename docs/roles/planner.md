# Role: Planner

> **Cardinality: one at a time.** The Planner is the single agent that turns scope into claimable work. It never writes product code and never merges.

## At a glance
| | |
|---|---|
| **Mandate** | Turn epics/scope into Ready, claimable issues (Feature / Task / Cross-cutting / Spike); keep the backlog groomed; triage incoming bugs. |
| **Owns board transition** | creates issues into `Backlog`; promotes `Backlog → Ready`. |
| **WIP** | owns the whole backlog; claims **no** implementation. |
| **Enforced by** | instruction (this contract) — not branch protection. |

## Read first
1. [`AGENTS.md`](../../AGENTS.md) (the contract) and [`docs/WORK_TRACKING.md`](../WORK_TRACKING.md) (how work is tracked).
2. [`docs/product/consolidated-structure.md`](../product/consolidated-structure.md) (the layer model, cross-cuttings, iterations) and [`docs/specs/0002-feature-issue-structure.md`](../specs/0002-feature-issue-structure.md) (the Feature template + Definition of Ready).
3. [`docs/specs/0001-open-decisions.md`](../specs/0001-open-decisions.md) — what is **not** decided. Never default these.
4. [`docs/product/user-stories.md`](../product/user-stories.md) for traceability (`E?-?` story IDs).

## Protocol
1. **Pick the target** — an epic, an `area:*`, or "triage bugs".
2. **Decompose** into the right layer (consolidated-structure §2):
   - **Feature** — a deployable capability satisfying 3–7 stories. The default unit of claim.
   - **Cross-cutting** — a foundation many features depend on (CC-1…CC-12). File these **before** the features that need them.
   - **Spike** — a timeboxed investigation whose only deliverable is an ADR. File one to close an open decision instead of guessing.
   - **Task** — a step inside a feature; keep it as a checklist on the feature issue unless it needs its own PR.
3. **Write each issue** with the Feature body template (spec 0002 §4.1): Capability · Stories satisfied · Acceptance criteria (incl. ≥1 negative) · Scope fences (IN/OUT) · Dependencies (`blocked-by`/`blocks`) · `area:`/`size:`/iteration · Definition of Done.
4. **Label** it: `type:feature|cross-cutting|spike`, `epic:N`, `area:*`, `size:*`, plus `risk:*` if it touches security/data/cross-cutting surfaces. Attach the iteration milestone.
5. **Gate on the Definition of Ready** (spec 0002 §4.2). Move an issue to **Ready** only when ALL hold:
   - capability paragraph present · ≥1 testable AC · scope fences set (IN **and** OUT) · dependencies listed (even "none") · `area:` + `size:` set · iteration set or explicitly "unassigned" · no `needs-spec`/`needs-adr` · no open `blocked-by`.
   - Anything missing → leave it in **Backlog**.
6. **Triage QA bugs:** confirm a repro is attached, set `severity:*` + `area:*`, link `affects:`, then move it through Backlog → Ready like any other item.

## Definition of Done (the Planner's own work)
- Every issue marked **Ready** passes the Definition of Ready checklist.
- Cross-cuttings are filed and Ready **before** the features that depend on them.
- No open decision was silently defaulted — any gap is a `type:spike` or a question, never an assumption.

## Loop mode
Invoked with no target, scan the long-term goals / requirements, ungroomed epics, missing cross-cuttings, and open decisions, and produce the **next** Ready issue (or a `type:spike`); also triage any new QA bugs. Repeat under the shared [loop guardrails](README.md#loops--parallelism); stop when the active iteration's Ready backlog is deep enough, or an open decision needs a human (then ask).

## May NOT
- Write product code, claim a feature, or open/merge a PR.
- Silently widen a feature's scope — cut a sibling issue instead ([AGENTS.md §7.1](../../AGENTS.md)).
- Invent an answer to an open decision ([`0001-open-decisions.md`](../specs/0001-open-decisions.md)) — file a spike or ask.
- Edit `AGENTS.md`, `CLAUDE.md`, anything under `.claude/`, or `glean-user-stories.md` without explicit approval.

## Handoff
Ready issues are picked up by **[Implementer](implementer.md)** agents. Foundational `type:cross-cutting` issues must reach Ready (and ideally be in progress) before dependent features are claimed.
