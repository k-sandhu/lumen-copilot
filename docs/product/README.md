# Product Research And User Stories

Status: candidate product-scope input.
Last updated: 2026-06-16.
Tracking issue: https://github.com/k-sandhu/lumen-copilot/issues/1.

These docs collect research and candidate user stories for Lumen Copilot. They are intentionally written as product discovery input, not as final product scope. They do not close OD-1 in `docs/specs/0001-open-decisions.md`; that still requires human confirmation and a dedicated scope/spec decision.

## Files

These two files are the **consolidated, comprehensive** product-discovery docs. They merge the root research notes (below) with a 22-vendor competitive sweep.

- [knowledge-work-automation-research.md](knowledge-work-automation-research.md) — research synthesis on Glean and ~22 adjacent enterprise-AI products, the seven-layer category pattern, category-wide shifts, capability deep-dives, metrics, mental models, candidate product shape, feature taxonomy, prioritization, and risks. **Vendor and product names are retained here** (it's research).
- [user-stories.md](user-stories.md) — comprehensive candidate user stories grouped by 16 epics, persona, and acceptance criteria. **Vendor/product names are deliberately removed** (capability-level, product-agnostic). Written in the `scripts/stories-to-issues.ps1`-parseable format (`# EPIC <n> — …`, `**E<n>-<m>.** As a **TAG**, …`, bolded persona legend) so it can seed issues via `-StoriesFile docs/product/user-stories.md`.

- [feature-build-plan.md](feature-build-plan.md) - dry-run plan for structuring epics, story issues, cross-cutting contract issues, labels, and workstreams for high-parallelism feature delivery.

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
