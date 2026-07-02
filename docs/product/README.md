# Product Research And User Stories

Status: product-discovery corpus — **OD-1 now closed** (via [spec 0003](../specs/0003-product-scope-and-mission.md)); retained as traceability + roadmap.
Last updated: 2026-07-02.
Tracking issue: https://github.com/k-sandhu/lumen-copilot/issues/1.

These docs collect research and candidate user stories for Lumen Copilot. They began as product-discovery input; **OD-1 (product scope) closed 2026-06-17** ([spec 0003](../specs/0003-product-scope-and-mission.md)) using them as the source corpus. They remain the **traceability + roadmap** layer: the MVP and the committed agents program are carved from them, and the rest is the sponsor-gated roadmap. The stories are the *why* behind Features on the board — not the unit of claim (that's the Feature; [spec 0002](../specs/0002-feature-issue-structure.md)). `user-stories.md` now carries **17 epics** (EPIC 17 added 2026-07-02).

## Files

These two files are the **consolidated, comprehensive** product-discovery docs. They merge the root research notes (below) with a 22-vendor competitive sweep.

- [knowledge-work-automation-research.md](knowledge-work-automation-research.md) — research synthesis on Glean and ~22 adjacent enterprise-AI products, the seven-layer category pattern, category-wide shifts, capability deep-dives, metrics, mental models, candidate product shape, feature taxonomy, prioritization, and risks. **Vendor and product names are retained here** (it's research).
- [user-stories.md](user-stories.md) — comprehensive candidate user stories grouped by 17 epics (EPIC 17 added 2026-07-02), persona, and acceptance criteria. **Vendor/product names are deliberately removed** (capability-level, product-agnostic). Written in the `scripts/stories-to-issues.ps1`-parseable format (`# EPIC <n> — …`, `**E<n>-<m>.** As a **TAG**, …`, bolded persona legend) so it can seed issues via `-StoriesFile docs/product/user-stories.md`.

- [feature-build-plan.md](feature-build-plan.md) - dry-run plan for structuring epics, story issues, cross-cutting contract issues, labels, and workstreams for high-parallelism feature delivery.
- [consolidated-structure.md](consolidated-structure.md) — **multi-model reconciliation** (Claude Opus 4.8 + gpt-5.5 + minimax-m3): the unified Epic→Feature→Story→Cross-cutting→Spike structure, reconciled labels/milestones/board, workstreams, and build sequencing.

## Inputs Used

- Public product material from Glean and comparable enterprise AI / work-automation companies (sources listed in the research doc).
- Existing local research notes at the repository root, consolidated into the two files above:
  - `glean-user-stories.md` — deep Glean teardown (**read-only here**; owned by the parallel story-finalization effort per `AGENTS.md` §7.9 and `docs/WORK_TRACKING.md` — not edited).
  - `knowledge-work-automation-user-stories.md` — 22-vendor competitive sweep + Epics 7–15 (carries a pointer to this consolidated set).

## How To Use These Docs

Use this folder as a structured backlog seed:

1. Review the research synthesis with a human product owner.
2. Decide which mission, audience, and scope statements are actually in-bounds.
3. Promote accepted capabilities into a product spec that closes OD-1.
4. Generate one tracked issue per accepted story or capability.
5. Keep rejected or deferred themes visible as explicit out-of-scope decisions.

## Story Conventions

Stories use this shape:

`As a [persona], I want [capability], so that [benefit].`

Acceptance criteria are written to be implementation-agnostic because the application stack is still undecided. Common criteria such as permission inheritance, citations, auditability, and approval gates should be applied to every story where relevant.
