# Feature Build Issue Plan

> **⚠️ SUPERSEDED — historical record (2026-06-16 dry run).** This document describes the repo *before* the stack landed: it says the repo is in "bootstrap mode", the board "has one issue in progress", and OD-1..OD-7 are all open. All of that is now false — OD-1..OD-5 closed, the stack shipped (M0–M2 done), and the board carries the full Feature/Cross-cutting/Spike backlog. Its enduring contributions (parallel workstreams, the cross-cutting *contracts* idea, the label taxonomy) were **reconciled and adopted** in [consolidated-structure.md](consolidated-structure.md) and [../specs/0002-feature-issue-structure.md](../specs/0002-feature-issue-structure.md). For the current operating model read [../WORK_TRACKING.md](../WORK_TRACKING.md). Kept unedited below as provenance for how the backlog structure was derived.

Status: dry-run planning document (superseded — see banner).
Prepared by: gpt-5.5.
Date: 2026-06-16.
Tracking issue: https://github.com/k-sandhu/lumen-copilot/issues/1.

This document captures a dry-run plan for structuring the Lumen Copilot feature backlog so many people and AI agents can work in parallel without colliding. It is based on the current product discovery docs and issue-generation script. It does not create issues, assign work, close product-scope decisions, or approve implementation.

## Current State

- The repository is still in bootstrap mode.
- Product scope, stack, architecture boundaries, security invariants, local-run path, CI, and agent/tool harness are open decisions in `docs/specs/0001-open-decisions.md`.
- The GitHub Projects board currently has one issue in progress: `#1 docs: product research and detailed user stories`.
- The story-to-issue script is dry-run by default and currently supports one story issue per parsed story.
- The curated story source is `docs/product/user-stories.md`; it is product-agnostic and formatted for the issue generator.

## Dry-Run Findings

Two candidate sources were tested without writing anything:

| Source | Dry-run result | Recommendation |
|---|---:|---|
| `glean-user-stories.md` | 163 story issues would be created | Use as raw research input only. |
| `docs/product/user-stories.md` | 136 story issues would be created | Use as the backlog seed. |

The curated `docs/product/user-stories.md` file is the better seed because it is consolidated, product-agnostic, and explicitly written for `scripts/stories-to-issues.ps1`.

Important blocker before execution: the current preview output showed milestones as only `EPIC`, so epic heading parsing should be fixed and re-verified before any real `-Execute` run.

## Recommended Backlog Shape

Use four levels of planning artifacts:

1. Milestones: one milestone per epic, for example `EPIC 1 - Enterprise Context Foundation`.
2. Epic issues: one tracking issue per epic with summary, child story checklist, dependency notes, and explicit boundaries.
3. Story issues: one issue per user story, small enough for one person or one AI agent to claim.
4. Cross-cutting contract issues: ADR/spec/gate issues for shared behavior used by many stories.

This keeps individual feature work parallel while making shared rules explicit instead of hidden inside whichever story happens to implement first.

## Candidate Epic Backlog

| Epic | Stories | Title |
|---:|---:|---|
| 1 | 8 | Enterprise Context Foundation |
| 2 | 10 | Unified Search And Trusted Answers |
| 3 | 13 | Assistant Workspace |
| 4 | 7 | Proactive Work Intelligence |
| 5 | 8 | Work Execution And Actions |
| 6 | 8 | Agent Builder, Library, And Reusable Skills |
| 7 | 8 | Autonomous, Scheduled, And Event-Driven Agents |
| 8 | 7 | Research, Analysis, And Evidence Work |
| 9 | 8 | Artifact And Content Creation |
| 10 | 7 | Meetings, Communication, And Follow-Up Intelligence |
| 11 | 9 | Knowledge Governance, Trust, And Source Quality |
| 12 | 13 | Departmental Automation |
| 13 | 9 | Security, Governance, Compliance, And Policy |
| 14 | 8 | Admin, Analytics, AgentOps, And Adoption |
| 15 | 9 | Developer Platform, Interoperability, And Extensibility |
| 16 | 4 | Computer Use And Browser/Desktop Automation |

Total from the curated source: 136 story issues.

## Parallel Workstreams

These workstreams let different people and agents claim separate areas while still coordinating through shared contracts.

| Workstream | Epics | Primary focus |
|---|---|---|
| Foundation | E1, E13, E14 | Sources, permissions, governance, audit, admin, AgentOps. |
| Search and answers | E2, E11 | Search, citations, trusted answers, source quality. |
| Assistant and productivity | E3, E4, E10 | Chat workspace, proactive intelligence, meetings, communication. |
| Actions and automation | E5, E7 | Writes, approvals, schedules, event triggers, escalation. |
| Agent builder and runtime | E6, E7, E14 | Builder, library, versions, traces, evaluation, autonomy. |
| Research and artifacts | E8, E9 | Deep research, analysis, memos, documents, generated artifacts. |
| Department packs | E12 | Sales, support, IT, HR, engineering, product, marketing, legal, finance, exec workflows. |
| Developer platform | E15 | APIs, SDKs, connectors, tools, events, embedding, sandboxing. |
| Browser and desktop automation | E16 | UI automation, browser agent, desktop agent, self-correction. |

## Cross-Cutting Contract Issues

Create these before or alongside story issues so parallel feature teams do not invent incompatible behavior:

- Product scope and mission spec to close OD-1.
- Tech stack ADR to close OD-2.
- Architecture boundaries ADR to close OD-3.
- Security and domain invariants spec to close OD-4.
- Local-run and verification ADR to close OD-5.
- Permission model contract for retrieval, citations, generated artifacts, and actions.
- Citation and provenance contract for answers, summaries, artifacts, and research.
- Audit event taxonomy for search, answer, agent run, tool call, approval, and write action.
- Approval and risk-tier contract for consequential actions.
- Freshness and sync-status contract for sources and answers.
- Evaluation and feedback contract for poor answers, unsafe actions, stale content, and agent quality.

## Labels For Parallelism

Recommended labels:

- `type:epic`
- `type:user-story`
- `type:adr`
- `type:spec`
- `type:docs`
- `type:chore`
- `blocked`
- `needs-adr`
- `needs-spec`
- `track:foundation`
- `track:search`
- `track:assistant`
- `track:actions`
- `track:agents`
- `track:research`
- `track:artifacts`
- `track:department-pack`
- `track:governance`
- `track:developer-platform`
- `track:browser-desktop`
- `risk:security`
- `risk:data`
- `risk:cross-cutting`

The `track:*` labels give agents clean ownership lanes. The `risk:*` labels make shared review requirements visible.

## Execution Sequence

Recommended sequence before creating the full backlog:

1. Fix the issue generator so epic milestones preview as full milestone names.
2. Re-run the dry run against `docs/product/user-stories.md`.
3. Human-review the 16 epics and 136 stories as candidate scope.
4. Close or explicitly defer OD-1 with a product scope/spec decision.
5. Create cross-cutting contract issues for permissions, citations, audit, approval, freshness, and evaluation.
6. Create one epic issue per accepted epic.
7. Create one story issue per accepted story.
8. Add each issue to the Projects board with labels, milestone, and status `Todo`.
9. Have each person or agent claim exactly one issue, move it to `In Progress`, and work on a dedicated branch.

## Parallel Work Rules

- One issue per branch.
- One assignee or agent owner per active issue.
- Do not widen a story issue if new scope appears; file a follow-up issue.
- Shared behavior must land through specs or ADRs, not ad hoc implementation.
- Cross-cutting contracts should be treated as dependencies by feature issues.
- Board status is the source of truth for claimed work.
- Story issues should carry acceptance criteria copied from the source story plus relevant global acceptance criteria.
- Any `[~]` or `[s]` status marker needs date, reason, and residual risk.

## Next Gate

Before running `scripts/stories-to-issues.ps1 -Execute`, fix and validate the milestone parsing issue, then get human confirmation that `docs/product/user-stories.md` is the accepted backlog seed.
