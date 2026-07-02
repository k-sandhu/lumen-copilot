# Lumen Copilot — Consolidated Feature / Epic / Story Structure (multi-model reconciliation)

Status: **adopted** (was discovery input; the structure it proposed is now the operating model).
Last updated: 2026-07-02.
Tracking issue: https://github.com/k-sandhu/lumen-copilot/issues/3.

> **Adoption status (2026-07-02).** When written this was discovery input; it has since been adopted. OD-1 closed using this doc as an input ([spec 0003](../specs/0003-product-scope-and-mission.md)); the layer model (§2), reconciled labels (§8), and 6-state board (§9) are live ([spec 0002](../specs/0002-feature-issue-structure.md) is marked adopted). **Divergences from what's below:** milestones are **thematic waves** with descriptive names (`M2 Trust Surfaces`, `M3 Agents & Extensibility`, `M2.5 Stabilization`) rather than §10's numbered ladder, though the *sequencing logic* (read-only value first, write-back behind approval, autonomy last) held. The §11 generator blocker is **moot** — the fleet cuts Features by hand (the mass story→issue path was not taken; see [../WORK_TRACKING.md](../WORK_TRACKING.md)). The story corpus grew to **17 epics** (EPIC 17 appended 2026-07-02). The current backlog beyond the committed M3 program is a set of **sponsor-gated next-wave epics** (connector breadth, knowledge trust, collaboration/trust, actions, proactive, research, AgentOps) mapped onto the roadmap below.

This document **reconciles the product-discovery planning artifacts authored in parallel by three different models** into one coherent structure for building the product. It was discovery input; adopting its board/label/milestone changes was done via tracked `type:chore`/`type:docs` issues + PRs per [AGENTS.md](../../AGENTS.md) §7 (fleet-tooling [#7](https://github.com/k-sandhu/lumen-copilot/issues/7)/[#9](https://github.com/k-sandhu/lumen-copilot/issues/9)).

## 1. Inputs reconciled (who contributed what)

| Source artifact | Model | Contribution (its "layer") |
|---|---|---|
| [user-stories.md](user-stories.md) + [knowledge-work-automation-research.md](knowledge-work-automation-research.md) | **Claude Opus 4.8** | The **backlog**: 16 epics / 136 vendor-neutral stories + the competitive research behind them. The *what*. |
| [feature-build-plan.md](feature-build-plan.md) | **gpt-5.5** | The **build plan**: parallel workstreams, cross-cutting *contracts*, label set, execution sequence, dry-run counts. The *how to parallelize*. |
| [../specs/0002-feature-issue-structure.md](../specs/0002-feature-issue-structure.md) | **minimax-m3** | The **issue meta-structure**: 6-layer Epic→Feature→Task→Cross-cutting→Spike model, Feature as unit-of-claim, Ready/Done gates, CC-1..CC-12 platform foundations, milestone/board reorg. The *unit of work*. |

These are **complementary, not competing**. Opus says *what to build*; minimax says *what shape the work items take*; gpt-5.5 says *how to run them in parallel*. This doc fuses them and resolves the few overlaps (milestone semantics, label sets, cross-cutting lists).

## 2. The reconciled layer model

Adopted from minimax-m3 §3, with the Opus backlog supplying the Story layer.

| Layer | What | Issue type | Unit of claim? | Source |
|---|---|---|---|---|
| **0 Strategy** | Mission, personas, capability map, research | docs in `docs/` | n/a | Opus research + OD-1 |
| **1 Epic** | Strategic pillar (16 of them) | `type:epic` | no (sponsor owns) | Opus backlog |
| **2 Feature** | Deployable capability satisfying 3–7 stories | `type:feature` | **YES — one agent owns one** | cut from stories |
| **3 Task** | Step inside a feature | checklist on the feature issue | no | claiming agent |
| **4 Cross-cutting** | Foundation many features depend on | `type:cross-cutting` | yes — platform owner, claimed first | gpt-5.5 + minimax (§5) |
| **5 Spike** | Timeboxed investigation → an ADR | `type:spike` | yes — separate WIP slot | minimax (§6) |

**Key reconciliation:** the **136 stories are traceability (the *why*)**, not the work unit. The **Feature is the claim unit** (~30–40 total). **Cross-cuttings and Spikes are first-class issues that stories never surface** — they're how foundations and open decisions get front-loaded.

## 3. Canonical backlog — 16 epics / 136 stories

Source of truth: [user-stories.md](user-stories.md) (generator-parseable). Workstream/area assignments reconcile gpt-5.5's workstreams with minimax's `area:*` labels.

| Epic | Title | Stories | Workstream (gpt-5.5) | Primary `area:*` (minimax) |
|---:|---|---:|---|---|
| 1 | Enterprise Context Foundation | 8 | Foundation | connectors, ingestion, permissions |
| 2 | Unified Search And Trusted Answers | 10 | Search & answers | search, citations |
| 3 | Assistant Workspace | 13 | Assistant & productivity | agents, ui, model-gateway |
| 4 | Proactive Work Intelligence | 7 | Assistant & productivity | proactive |
| 5 | Work Execution And Actions | 8 | Actions & automation | actions |
| 6 | Agent Builder, Library, Reusable Skills | 8 | Agent builder & runtime | agents |
| 7 | Autonomous, Scheduled & Event-Driven Agents | 8 | Actions & automation | agents, scheduling |
| 8 | Research, Analysis & Evidence Work | 7 | Research & artifacts | research |
| 9 | Artifact And Content Creation | 8 | Research & artifacts | artifacts |
| 10 | Meetings, Communication & Follow-Up | 7 | Assistant & productivity | meetings |
| 11 | Knowledge Governance, Trust & Source Quality | 9 | Search & answers | knowledge-quality |
| 12 | Departmental Automation | 13 | Department packs | department-pack |
| 13 | Security, Governance, Compliance & Policy | 9 | Foundation / Governance | permissions, governance |
| 14 | Admin, Analytics, AgentOps & Adoption | 8 | Foundation / Agents | observability, admin |
| 15 | Developer Platform & Interoperability | 9 | Developer platform | api, developer-platform |
| 16 | Computer Use & Browser/Desktop Automation | 4 | Browser & desktop | browser-desktop |
| | **Total** | **136** | 9 workstreams | 16 areas |

Per-story detail (persona, AC) lives in `user-stories.md`; the dry-run issue plan is in [feature-build-plan.md](feature-build-plan.md).

## 4. Feature layer — the unit of claim

Features are cut from stories (3–7 each), giving **~30–40 features**. Use minimax-m3's feature body template (§4.1): Capability · Stories satisfied · Acceptance criteria (incl. negative) · Scope fences (IN/OUT) · Dependencies (blocked-by/blocks) · area/size/iteration · Definition of Done. A feature is **claimable only when it passes the Definition of Ready** (capability + ≥1 testable AC + scope fences + deps listed + area/size set + no `needs-spec` + no open `blocked-by` + assignee).

**Worked example — how stories roll into features (illustrative, first two epics):**

| Feature | Stories | area | Depends on (cross-cutting) |
|---|---|---|---|
| F1.1 Connector onboarding & sync health | E1-1, E1-3 | connectors | CC-4 |
| F1.2 Retrieval-time permission enforcement | E1-2, E1-7 | permissions | CC-1, CC-5 |
| F1.3 Work context graph & entities | E1-4 | ingestion | CC-5 |
| F1.4 Custom connector SDK | E1-5 | connectors | CC-4 |
| F1.5 Structured + unstructured unification | E1-6 | ingestion | CC-5 |
| F1.6 Code-aware indexing | E1-8 | connectors | CC-4 |
| F2.1 Unified permissioned search + previews | E2-1, E2-3 | search | CC-1 |
| F2.2 Cited direct answers + feedback | E2-2, E2-10 | citations | CC-11 |
| F2.3 Filters & operators | E2-4 | search | — |
| F2.4 People / expert / org | E2-5, E2-6 | identity | CC-3 |
| F2.5 Decisions & conflict surfacing | E2-7, E2-8 | search | CC-11 |
| F2.6 Shortcuts & collections | E2-9 | search | — |

Epics 3–16 decompose the same way; the full feature cut is deferred until each epic is groomed (and is iteration-scoped, §9).

## 5. Cross-cutting foundations (reconciled)

gpt-5.5 listed cross-cutting **contracts** (specs); minimax listed cross-cutting **platform components** (CC-1..CC-12). They are the **two faces of the same thing**: the contract is the *spec*, the component is the *implementation*. Reconciled into one table.

| CC | Platform component (build) | Governing contract (spec/ADR) | Unblocks |
|---|---|---|---|
| **CC-1** | Permission / ACL enforcement at retrieval + action time | Permission model contract | most of E1, E2, E3, E5 |
| **CC-2** | Tenancy & data isolation | (security invariants — OD-4) | all features |
| **CC-3** | Identity: SSO + SCIM + per-user identity graph | (identity contract) | E1-4/E2-5/E2-6, E13 |
| **CC-4** | Connector framework (managed + push) | Freshness & sync-status contract | E1, E4, E15 |
| **CC-5** | Ingestion skeleton (ACL mirror, change detection, scheduling) | Freshness & sync-status contract | E1, E4, E7 |
| **CC-6** | Agent runtime (planning, sub-agents, tool-calling, sandbox) | (agent-runtime contract) | E3, E6, E7, E8 |
| **CC-7** | Action / tool-call gateway (read + write, approval flow) | Approval & risk-tier contract | E5, E7 |
| **CC-8** | Audit log + event stream to SIEM | Audit event taxonomy | E13, E14 |
| **CC-9** | Model gateway (per-chat/step model choice, governance) | (model-governance contract) | E3, E13 |
| **CC-10** | Observability (traces, eval/LLM-judge feedback, ROI) | Evaluation & feedback contract | E14 |
| **CC-11** | Citations & provenance (passage-level) | Citation & provenance contract | E2, E3, E8, E9 |
| **CC-12** | Storage & file sandbox (uploads, retention) | (data-retention contract) | E3, E15 |

Cross-cuttings are **claimed before the features that depend on them** — minimax calls this "the single largest parallelization unlock." The list is stack-shaped and will be re-confirmed once OD-2/OD-3 close.

## 6. Spikes — closing the open decisions

A spike is a timeboxed investigation whose only deliverable is an ADR/spec. These unblock features without blocking them.

| Spike | Closes | Deliverable |
|---|---|---|
| Product scope & mission | **OD-1** | spec (`docs/specs/`) |
| Tech stack | **OD-2** | ADR (`docs/architecture/`) |
| Architecture boundaries & adapters | **OD-3** | ADR |
| Security & domain invariants (→ negative-test set) | **OD-4** | spec |
| Local-run path + `/verify` gates | **OD-5** | ADR |
| `.claude/` harness | **OD-6** | config + ADR |
| CI parity | **OD-7** | CI workflow |

## 7. Workstreams ↔ areas ↔ ownership (single lane map)

Reconciles gpt-5.5's 9 workstreams with minimax's `area:*` labels. This is the table an agent uses to find their lane.

| Workstream (gpt-5.5) | `area:*` (minimax) | Epics | Owns cross-cuttings |
|---|---|---|---|
| Foundation | connectors, ingestion, permissions, identity, tenancy | E1, E13, E14 | CC-1..CC-5, CC-8 |
| Search & answers | search, citations, knowledge-quality | E2, E11 | CC-11 |
| Assistant & productivity | agents(chat), ui, proactive, meetings, model-gateway | E3, E4, E10 | CC-6, CC-9, CC-12 |
| Actions & automation | actions, scheduling | E5, E7 | CC-7 |
| Agent builder & runtime | agents, builder | E6, E7, E14 | CC-6, CC-10 |
| Research & artifacts | research, artifacts | E8, E9 | — |
| Department packs | department-pack | E12 | (consumes all) |
| Developer platform | api, developer-platform | E15 | CC-4 (SDK) |
| Browser & desktop | browser-desktop | E16 | CC-7 |

> `track:*` labels (gpt-5.5) and `area:*` labels (minimax) are the same concept; reconciled to **`area:*`** (build-side) alongside the existing **`persona:*`** (user-side). `track:*` is dropped as a redundant alias.

## 8. Reconciled label taxonomy

| Category | Labels | Status |
|---|---|---|
| Work type (existing) | `type:epic` `type:user-story` `type:adr` `type:docs` `type:chore` `type:bug` | **exist** |
| Work type (proposed) | `type:feature` `type:cross-cutting` `type:spike` | propose (Phase A) |
| Strategic grouping | `epic:1..16` | runtime (generator) |
| User lane | `persona:<tag>` (23) | runtime (generator) |
| Build lane | `area:search` `area:agents` `area:connectors` `area:permissions` `area:ingestion` `area:ui` `area:api` `area:observability` `area:identity` `area:model-gateway` `area:citations` `area:storage` `area:proactive` `area:meetings` `area:knowledge-quality` `area:actions` `area:scheduling` `area:builder` `area:research` `area:artifacts` `area:department-pack` `area:developer-platform` `area:browser-desktop` | propose (Phase A) |
| Sizing / deps | `size:S\|M\|L` · body refs `blocks:#N` / `blocked-by:#N` · `affects:epic-N` | propose (Phase A) |
| Risk (review gates) | `risk:security` `risk:data` `risk:cross-cutting` | propose (Phase A) |
| State | `blocked` `needs-spec` `needs-adr` `good-first-issue` | `needs-adr` new; rest exist |

No existing label is removed or renamed.

## 9. Milestones, iterations & board (reconciled)

**The one real conflict between inputs, resolved here.** The current generator and gpt-5.5 use **milestone = epic** (`EPIC n - title`). minimax proposes **milestone = iteration, epic = label** (so the board can answer "what's demoable this iteration?"). GitHub allows one milestone per issue.

**Reconciled recommendation:** adopt **iteration-as-milestone + `epic:N`-as-label**, migrated incrementally (minimax §7.3, path "b"):
- Keep the existing per-story issues on their `epic:N` label.
- Introduce **iteration milestones** as Features are cut: `M0 Foundations` · `M1 Search v0` · `M2 Assistant v0` · `M3 Actions+Agents v0` · `M4 Governance & AgentOps` · `M5 Frontier & Platform`.
- Widen the board Status field from `Todo/In Progress/Done` to **`Backlog → Ready → In Progress → In Review → Blocked → Done`** (minimax §7.4); only **Ready** items are claimable.

## 10. Build sequencing (reconciled phases → iterations)

Fuses the Opus 6-phase plan, gpt-5.5's execution sequence, and minimax's adoption rollout into one iteration ladder. Cross-cuttings are claimed **first** in each iteration; spikes run in parallel WIP slots.

| Iteration | Theme | Claim first (cross-cutting) | Then (epics/features) | Spikes / gates |
|---|---|---|---|---|
| **M0 Foundations** | permissioned context + audit | CC-1, CC-2, CC-3, CC-4, CC-5, CC-8 | E1, E13 core (E13-1/2/6) | OD-2, OD-3, OD-4, OD-5 |
| **M1 Search v0** | cited, trusted search | CC-11 | E2, E11 starter (E11-1/2) | — |
| **M2 Assistant v0** | grounded chat + proactive | CC-6, CC-9, CC-12 | E3, E4, E10 | — |
| **M3 Actions & Agents v0** | approval-gated writes + builder | CC-7, CC-10 | E5, E6, E7, E14 (inventory/traces) | — |
| **M4 Governance & AgentOps** | trust + fleet ops | (harden CC-8, CC-10) | E11 advanced, E13 advanced, E14, E8, E9 | OD-6, OD-7 |
| **M5 Frontier & Platform** | extensibility + risk surfaces | (per feature) | E15, E16, E12 department packs, polish | — |

This mirrors the research doc's "good-early vs riskier-later" split: read-only value first (search, summaries, drafts), write-back behind approval next, autonomous/computer-use last.

## 11. Known blocker before any real issue generation

Both the Opus dry run and gpt-5.5's plan caught this: the issue generator currently previews **milestones as bare `EPIC`**. Root cause (confirmed): `scripts/stories-to-issues.ps1` has **no UTF-8 BOM**, so Windows PowerShell 5.1 loads it as ANSI and the **em-dash (`—`) in the epic-heading regex breaks** → epic headings don't match (stories and personas still parse, so the 136 count is correct). Fix one of:
1. Re-save `scripts/stories-to-issues.ps1` as **UTF-8 with BOM** (fixes every file), or
2. Use ASCII hyphens `-` in `# EPIC n -` headings, or
3. Run the generator under `pwsh` (PowerShell 7+), which reads scripts as UTF-8.

Then re-run `stories-to-issues.ps1 -StoriesFile docs/product/user-stories.md` (dry-run) and confirm milestones render with their full titles before `-Execute`.

## 12. Adoption rollout (reconciled, from minimax §11 — each step is its own tracked PR)

- **Phase A — labels only:** add `type:feature/cross-cutting/spike`, `area:*`, `size:*`, `risk:*`, `needs-adr` to `scripts/setup-board-and-labels.ps1`. (`type:chore`)
- **Phase B — milestone reorg:** switch generator milestone semantics to iteration; `epic:N` becomes a label. (`type:chore`)
- **Phase C — board widening:** Status field → 6 states. (`type:chore`)
- **Phase D — first features + cross-cuttings:** open 2–3 `type:feature` + 2–3 `type:cross-cutting` (M0), claim in parallel.
- **Phase E — re-parent legacy stories** under features (optional, deferred).

## 13. Decided vs still open

- **Decided here:** the *shape* of the backlog and how work items are structured (layers, features-as-claim-unit, reconciled cross-cuttings, labels, iteration milestones, sequencing).
- **Still open (do not default — ask):** **OD-1** product scope/mission, **OD-2** stack, **OD-3** boundaries, **OD-4** security invariants, **OD-5** local-run/verify, **OD-6** harness, **OD-7** CI. This structure is **parallel-safe but execution-gated** on OD-4/OD-5 (the proof-of-done gate). Nothing in this document closes a decision.

---

## Provenance

- **Reconciled by:** Claude Opus 4.8, 2026-06-16, from the three model-authored inputs in §1.
- **Models reconciled:** Claude Opus 4.8 (backlog + research), gpt-5.5 (feature-build-plan), minimax-m3 (feature/issue spec 0002).
- **Status:** candidate product-scope input — **awaiting human sign-off**; does not close OD-1 or adopt any board/label change (those are §12 tracked PRs).
- **Traceability:** issue #3; this doc is the deliverable for that issue.
