# Spec 0002 — Feature/Issue Structure (for parallel human + agent work)

> Playbook rule: *structure is a force multiplier for parallelism only if it makes dependencies, scope, and the unit of claim explicit.* This spec proposes a 6-layer model that turns the existing 1-story-→-1-issue pipeline into a board that multiple humans and AI agents can work from concurrently without collision.
>
> **Status:** proposed (draft for review, **not yet adopted**). Adopting it requires the label/milestone/board changes in §7 and a human sign-off on the open decisions listed in §10. **Last reviewed:** 2026-06-16.

---

## 1. Context

Today's pipeline ([scripts/stories-to-issues.ps1:124-129](scripts/stories-to-issues.ps1:124-129)) turns each `E?-?` story in [glean-user-stories.md](glean-user-stories.md) into one issue on the `EPIC <n> - <title>` milestone. That is correct for **traceability** but caps **parallelism** in three concrete ways:

1. **Wrong unit of claim.** Many stories are 1-paragraph capabilities that span 5–10 engineering tasks. E1-1 alone (universal search across all apps) touches the connector framework, ranking, ACL mirroring, query parser, and UI — six agents cannot all own E1-1.
2. **Invisible foundations.** Auth, tenancy, ingestion skeleton, ACL enforcement, audit logging, the connector framework, the agent runtime, the event bus, observability, the model gateway — none of these are user stories, so the current model never files an issue for them. Features can't all start in parallel until they exist.
3. **Milestone = epic conflates scope with cadence.** Every story in Epic 1 shares one milestone, so the board cannot answer "what's demoable this iteration?"

This spec leaves OD-1 (product scope) and OD-2 (tech stack) explicitly open and proposes a **meta-structure** that makes whatever scope is chosen parallel-safe.

## 2. Goals & non-goals

**Goals**
- Make the **unit of claim** a deployable, demoable capability that one agent can own end-to-end.
- Make **foundations visible and front-loadable** as first-class issues.
- Make **dependencies explicit** so an agent's daily queue can be derived from a saved view.
- Preserve **traceability to user stories** (they become the *why*, not the *what*).
- Be adoptable **incrementally** — features, cross-cuttings, and spikes can be created under the new model while legacy `type:user-story` issues are gradually closed or re-parented.

**Non-goals**
- Choosing the product mission, scope, or "what is Lumen Copilot" (still OD-1, [docs/specs/0001-open-decisions.md](docs/specs/0001-open-decisions.md)).
- Choosing the tech stack (still OD-2).
- Drawing architecture boundaries (still OD-3) — the spec only *invokes* the adapter principle; it does not draw the lines.
- Replacing `AGENTS.md` or any contract file. This spec defers to [AGENTS.md](../AGENTS.md) on all process rules.

## 3. The layer cake

| Layer | What | Issue type | Unit of claim? | Created by |
|---|---|---|---|---|
| **0. Strategy** | Vision, mission, personas, capability map, ADRs | docs in `docs/` | n/a | hand + OD-1 close |
| **1. Epic** | Strategic pillar (Search, Chat, Agents, Permissions, …) | `type:epic` | no — owner is a *sponsor* | one per pillar, before features |
| **2. Feature** | Deployable, demoable capability; satisfies 3–7 stories; owns 1 area | `type:feature` | **YES — one agent owns one** | derived from stories once epic is signed off |
| **3. Task** | Step inside a feature | Markdown checklist **on the feature issue** (default); `type:task` only when a step needs its own PR | no — owned by the feature's agent | the claiming agent |
| **4. Cross-cutting** | Foundation many features depend on (ACL, tenancy, audit, ingestion, agent runtime, event bus, observability, identity, model gateway, …) | `type:cross-cutting` | yes — **platform owner, claimed first** | surfaced by epic grooming, not by stories |
| **5. Spike** | Timeboxed investigation whose deliverable is **an ADR** (not code) | `type:spike` | yes — separate WIP slot, timeboxed | needed to close OD-2/OD-3/OD-5 |

User stories become the **traceability trail** — the feature body lists which `E?-?` stories it satisfies; the stories are not the work unit.

## 4. The feature — the unit of claim

### 4.1 Feature body template

```markdown
## Capability
<one paragraph: "as a [persona], I want [X], so that [Y]">

## Stories satisfied
- [ ] E1-1, E1-2   (links to story issues; traceability)

## Acceptance criteria  ← becomes the test plan
- [ ] AC-1: <observable, testable>
- [ ] AC-2: <observable, testable>
- [ ] AC-N (negative): <unauthorized → denied, wrong-role → forbidden,
        illegal state transition, malformed input, broken invariant>
        (see AGENTS.md §9 for the full negative-test set once OD-4 closes)

## Scope fences
- IN: …
- OUT: …  (move to a sibling feature if it grows; do not widen silently)

## Dependencies
- blocked-by: #<n>   (features / cross-cuttings)
- blocks:    #<n>

## Area / size / iteration
area:search   size:M   iteration:M2

## Definition of Done (per AGENTS.md §15)
- [ ] spec/ADR matches; test written first; /verify green;
      /verify-live round-trip + teardown (when the stack exists)
```

### 4.2 Definition of Ready (the gate that makes parallelism safe)

A feature is **claimable** only when **all** of the following are true:
- Capability paragraph present
- ≥1 AC written and testable
- Scope fences set (IN and OUT both populated)
- Dependencies listed (even if "none")
- `area:` and `size:` set
- `iteration:` set or explicitly "unassigned"
- No `needs-spec` label
- No open `blocked-by` references
- The claiming agent has been assigned (claim before editing — AGENTS.md §7.2)

Anything missing sits in **Backlog** until it's filled out. This is the largest behavioral change: an agent can no longer "just grab something off the board." That cost is the price of safe parallel work.

### 4.3 Definition of Done

Inherits [AGENTS.md §15](../AGENTS.md) verbatim. The feature is **Done** only when:
- PR is merged (not just open).
- Every AC checkbox is checked, with the verification noted in the PR body or a linked test run.
- Affected docs and ADRs are updated in the **same** PR.
- The board item moves to **Done**.

## 5. Cross-cuttings — the parallelism unlock

Cross-cuttings are foundations that *many* features depend on. They are **not** derivable from user stories (stories are user-facing; foundations are not), so they must be **surfaced during epic grooming**, not pulled out of `glean-user-stories.md`. They are claimed **before** the features that block on them, by the platform owner.

### 5.1 Likely cross-cuttings for this corpus (provisional, to be re-confirmed when OD-2 closes)

| ID (proposed) | Title | Approx. features it unblocks |
|---|---|---|
| CC-1 | Permission / ACL enforcement at retrieval time | every E1-*, E2-*, E3-* that returns data (most of E1, E2, E3) |
| CC-2 | Tenancy & data isolation (single-tenant boundary) | all features |
| CC-3 | Identity: SSO + SCIM + per-user identity graph | E1-13, E1-16, E2-25, E5-11, E5-12 |
| CC-4 | Connector framework (managed container + push) | E1-1, E1-2, E1-36, E4-1, E4-2, E4-3, E4-4 |
| CC-5 | Ingestion skeleton (ACL mirror, change detection, scheduling) | CC-4, E1-2, E4-2 |
| CC-6 | Agent runtime (planning, sub-agents, tool-calling, sandbox) | E3-1..E3-9, E3-13..E3-16, E2-16, E2-29 |
| CC-7 | Action / tool-call gateway (read + write, approval flow) | E3-17..E3-20, E6-* workflow agents |
| CC-8 | Audit log + event stream to SIEM | E5-18, E3-32, E3-33, E5-8, E5-9 |
| CC-9 | Model gateway (per-chat/per-step model choice, governance) | E2-22..E2-24, E3-8 |
| CC-10 | Observability (traces, LLM-judge feedback, ROI metrics) | E3-32, E3-33, E5-16, E5-17 |
| CC-11 | Citations & deep-linking (passage-level) | E2-2, E1-12, E2-29, E6-36 |
| CC-12 | Storage & file sandbox (uploads, retention) | E2-19..E2-21, E5-7 |

This list is **provisional** and will be re-confirmed (and almost certainly re-shaped) when OD-2 and OD-3 close — cross-cuttings are stack-shaped, and a different stack will produce a different fan-out.

## 6. Spikes — closing the open decisions

A spike is a **timeboxed investigation whose only deliverable is an ADR** (and the issues it unblocks). Spikes are how OD-2, OD-3, and parts of OD-4/OD-5 close without blocking features.

| Field | Value |
|---|---|
| Issue type | `type:spike` |
| Timebox | 1–2 days (S), 3–5 days (M); hard cap; time-boxed on the issue |
| WIP | separate slot from features and cross-cuttings |
| Output | one ADR in `docs/architecture/` + zero-or-more linked issues (features / cross-cuttings it unblocks) |
| Definition of Done | ADR merged **or** explicit "decision deferred" ADR with the reasons |

**Rule of thumb:** if a feature is blocked by an open decision and the decision needs >half a day to answer, file a spike — do not "just try something."

## 7. Labels / milestones / board

### 7.1 What already exists

Base structural labels ([scripts/setup-board-and-labels.ps1:33-44](scripts/setup-board-and-labels.ps1:33-44)):

```
type:user-story  type:epic  type:adr  type:docs  type:chore
type:bug  blocked  needs-spec  good-first-issue
```

At run time, [scripts/stories-to-issues.ps1:174-179](scripts/stories-to-issues.ps1:174-179) creates `epic:N` and `persona:*` from the stories file.

### 7.2 Proposed additions

| Label | Purpose | Why |
|---|---|---|
| `type:feature` | the new claimable unit | replaces `type:user-story` as the work unit; stories stay as traceability |
| `type:cross-cutting` | foundations blocking many features | makes invisible work visible and front-loadable |
| `type:spike` | timeboxed investigation → ADR | closes OD-2/3/5 without blocking features |
| `area:search` `area:agents` `area:connectors` `area:permissions` `area:ingestion` `area:ui` `area:api` `area:observability` `area:identity` `area:model-gateway` `area:citations` `area:storage` | **build-side** complement to `persona:*` | the filter an agent uses to find their lane |
| `affects:epic-1,epic-3` | fan-out tag for cross-cuttings | shows which epics the cross-cutting touches |
| `blocks:#N` / `blocked-by:#N` | explicit dep edges in body | surfaces via saved views; gates Definition of Ready |
| `size:S\|M\|L` | t-shirt sizing | drives WIP limits |

(All additions are new; none of the existing labels are removed or renamed.)

### 7.3 Milestone reorg (the structural change to the generator)

Today the script attaches every story to the **epic milestone** ([stories-to-issues.ps1:177-178](scripts/stories-to-issues.ps1:177-178)). That conflates *strategic scope* with *delivery cadence*. Recommended split:

- **`epic:N` becomes a label** (strategic grouping; no fan-out limit).
- **The milestone becomes the iteration** — `M0 Foundations`, `M1 Search v0`, `M2 Chat v0`, `M3 Agents v0`, `M4 Governance`, `M5 Polish`. The iteration is the demoable surface; the epic is the strategic pillar.

GitHub allows only one milestone per issue, so the choice is: epic OR iteration. The proposal is **iteration-as-milestone, epic-as-label**.

**Migration:** the existing `stories-to-issues.ps1` either (a) is adapted to set the `epic:N` label and leave the milestone unset until the iteration plan exists, or (b) is left alone for the existing user-story issues and a sibling script assigns iteration milestones as features are cut from stories. (b) is the lower-risk path; recommend (b) for the first quarter.

### 7.4 Board (Status field)

Replace 3-state with 6-state:

```
Backlog  →  Ready  →  In Progress  →  In Review  →  Blocked  →  Done
```

| Status | Meaning | Claimable? |
|---|---|---|
| **Backlog** | exists but not yet spec'd; missing AC, scope fences, or dependencies | no — only groomable |
| **Ready** | passes the Definition of Ready gate (§4.2) | **YES** |
| **In Progress** | assignee set, branch created, work started | (claimed) |
| **In Review** | PR open, CI running, reviewer assigned | (claimed) |
| **Blocked** | assignee set, waiting on a `blocked-by`; comment explains why | no |
| **Done** | PR merged, ACs checked | n/a |

The current board (#7 "Lumen Copilot") ships with `Todo / In Progress / Done`; widening the field is a one-time admin action.

## 8. WIP rules (so parallelism doesn't degrade into thrash)

- One agent = at most **1 feature + 1 cross-cutting** at a time (humans and AI agents alike).
- Two agents may not own issues sharing an `area:*` simultaneously unless the dependency edge is explicit *and* a reviewer is named.
- A `type:spike` is a **separate WIP slot**, timeboxed, ADR-or-bust.
- Cross-cuttings are claimed **before** the features that depend on them; this is the single largest parallelization unlock.
- Discovering a follow-up mid-task → file it as its own issue ([AGENTS.md §7.1](../AGENTS.md)), do not silently widen the current one.

## 9. Dry-run numbers for this corpus

| Source | Epics | Stories | Rough feature count | Likely cross-cuttings | Likely spikes |
|---|---|---|---|---|---|
| [glean-user-stories.md](../glean-user-stories.md) (E1–E6) | 6 | ~96 | ~20–25 | ~6–8 (subset of §5.1) | ~3–4 |
| [knowledge-work-automation-user-stories.md](../knowledge-work-automation-user-stories.md) (E7–E15) | 9 | ~70 | ~15–20 | ~3–4 (mostly re-used) | ~2–3 |
| **Consolidated** vendor-neutral map (`docs/product/user-stories.md`, referenced but not yet present) | ~15 | ~140–160 | **~30–40** | **~8–12** | **~5–8** |

Steady-state board size: **~50–70** issues (vs. ~150 flat stories). Iteration milestones carry the cadence; the rest is stable.

## 10. What this does NOT solve

| Open decision | Why it blocks | What unblocks it |
|---|---|---|
| **OD-1** product scope & mission adjectives | feature *prioritization* into iterations is a product call; the structure parallelizes the work but does not choose it | close OD-1 (spec) |
| **OD-2** tech stack | many cross-cuttings (vector store, model gateway, queue) are stack-shaped; features touching them will be re-classified | spikes → ADR(s) → close OD-2 |
| **OD-3** architecture boundaries | the adapter principle is invoked, not drawn; `area:permissions` etc. can be spec'd as contracts but their implementations diverge by stack | spikes → ADR(s) → close OD-3 |
| **OD-4** security & domain invariants | the full negative-test category set is still parked; "AC-N (negative)" in §4.1 is a placeholder | close OD-4 |
| **OD-5** local-run path | "Ready" assumes a claiming agent can verify locally; without `/verify` and `/verify-live`, "Done" is just "merged" — and the safest parallel work is the work you can prove | close OD-5 |

Until those close, this spec is **parallel-safe in structure, blocked in execution at the verification step**. The structure can be adopted now; the proof-of-done gate lands with OD-5.

## 11. How to adopt (proposed rollout)

This is a **proposal, not a commitment**. Per [AGENTS.md §7.1](../AGENTS.md), adopting any of the following is a substantive change and must be tracked by an issue on a branch via PR with `Closes #<N>`.

**Phase A — labels only (low risk, high signal):**
1. File a `type:chore` issue: "add `type:feature` / `type:cross-cutting` / `type:spike` / `area:*` / `affects:*` / `blocks:*` / `blocked-by:*` / `size:*` to base label set."
2. Patch [scripts/setup-board-and-labels.ps1](scripts/setup-board-and-labels.ps1) to add the new labels.
3. Land the PR. No existing issues move.

**Phase B — milestone reorg (medium risk):**
1. File a `type:chore` issue: "switch milestone semantics from epic-title to iteration."
2. Patch [scripts/stories-to-issues.ps1](scripts/stories-to-issues.ps1) to set `epic:N` as a label and leave milestone unset (or assign iteration milestones from a separate config).
3. Land the PR. **All existing story issues keep their current milestone** — do not retroactively reassign.

**Phase C — Status field widening (low risk, one-time):**
1. File a `type:chore` issue: "widen board Status from Todo/In Progress/Done to Backlog/Ready/In Progress/In Review/Blocked/Done."
2. Update the Projects board config.
3. Land the PR. Migration of existing items is mechanical: `Todo → Backlog` unless it already meets the Definition of Ready, in which case `Backlog → Ready`.

**Phase D — first features (parallelism-on moment):**
1. Open 2–3 `type:feature` issues, one per epic, with full bodies per §4.1.
2. Open 2–3 `type:cross-cutting` issues from §5.1.
3. Have multiple agents (or humans) claim in parallel.

**Phase E — re-parenting legacy stories (later, optional):**
- Once the feature model is stable, story issues become checklist items on the feature body, then close. This step is reversible and can be deferred indefinitely.

## 12. Open questions for the human sponsor

1. **OD-1:** do you want a vendor-neutral capability map drafted as a spec PR (`docs/product/user-stories.md`), or does the parallel story-finalization effort own it? (Per [AGENTS.md §7.9](../AGENTS.md), I do not edit `glean-user-stories.md` or `knowledge-work-automation-user-stories.md`.)
2. **Source-of-truth for the generator:** which file should `stories-to-issues.ps1` read by default? (The companion file already says the vendor-neutral rewrite is canonical, but it does not exist yet.)
3. **Phase ordering:** A → C → B → D, or A → B → C → D? (B before C means the milestone semantics change while the Status field is still 3-state, which is the more conservative ordering.)
4. **Cross-cutting ownership:** who is the platform owner for CC-1..CC-12? The first 3–4 cross-cuttings should be claimed and partially Done before the first feature claims land, or parallelism collapses.
5. **WIP limits:** confirm "1 feature + 1 cross-cutting" for both humans and AI agents, or relax for agents (e.g. "1 feature + 2 cross-cuttings") given agents don't context-switch in the same way humans do.

---

## Document origin

- **Authored by:** an AI assistant running model **`minimax/minimax-m3`** (display name `minimax-m3`).
- **Date:** 2026-06-16.
- **Purpose:** draft spec for the feature/issue structure described in a planning dry-run earlier in this session; **not** a binding decision.
- **Status:** **proposed — awaiting human sign-off** on §7 changes and §12 open questions.
- **Provenance:** content reflects the proposal in the conversation, refined into a self-contained spec. No product, stack, or architecture decision has been made here — those remain parked in [docs/specs/0001-open-decisions.md](docs/specs/0001-open-decisions.md).
- **Traceability:** adopting any of §11 requires a `type:chore` or `type:docs` issue + branch + PR per [AGENTS.md §7](../AGENTS.md). This document is not that PR.
