# 2. Multi-harness agent role model, orchestration & autonomous loops

- **Status:** Accepted
- **Date:** 2026-06-17

## Context
The repo is moving to a multi-agent operating model: one **Planner** creates work, many **Implementers** build it in parallel, one **Reviewer** merges, and **QA** tests merged `main` and files bugs (QA is post-merge / continuous). The fleet must run under **any** agent harness — Claude Code, OpenAI Codex, OpenCode, and others — which share `AGENTS.md` as their contract (Claude Code via the `CLAUDE.md` pointer). We need the roles defined **once**, invokable as first-class commands in each harness, without duplicating the rules per tool (duplication drifts). This is the slash-command + review-subagent slice of **OD-6**.

## Decision
- **Canonical role contracts live in `docs/roles/`** (`planner|implementer|reviewer|qa.md`) — tool-agnostic, the single source of truth, sitting on top of `AGENTS.md`.
- **Each harness gets thin wrappers** that only say *"read `AGENTS.md` + `docs/roles/<role>.md` and execute it"*:
  - Claude Code — `.claude/commands/*.md` (slash commands) + `.claude/agents/*.md` (subagents);
  - OpenCode — `.opencode/command/*.md` + `.opencode/agent/*.md`;
  - Codex — `.codex/prompts/*.md` (installed to `~/.codex/prompts/`).
- **Role separation is instruction-enforced**, not via branch protection (deliberate, for now). Two mechanical backstops: the Reviewer and QA subagents are declared **without Write/Edit** tools, and work is isolated **one branch + worktree per issue**.
- **QA is post-merge / continuous** — it files `type:bug` issues into the backlog; it is not a merge gate and owns no board column.
- **An Orchestrator role** (command-driven, *not* a subagent — it must spawn workers) fans a target (epic / milestone / "all Ready") into **parallel Implementers**, one per claimable item, cross-cuttings first, one per `area:*`, up to a parallelism budget. In-session it spawns workers in isolated git worktrees; cross-harness / CI it launches one headless process per item. `/implementer <epic>` delegates here.
- **Every role is loop-able**: invoked with no target it pulls its next unit of work from the board and repeats under shared guardrails (claim-as-mutex, empty-queue-is-a-clean-stop, never default an open decision, respect WIP, idempotent re-entry, bounded). Parallelism comes from the Orchestrator, not from one agent claiming many.

## Consequences
- A role is updated in **one file**; every harness inherits it. Adding a harness = adding thin wrappers, no rule duplication.
- This lands the **slash-command + subagent** portion of OD-6. The rest of OD-6 (permission tiers, hooks, auto-push) and the real enforcement gates (branch protection, required CI checks — OD-7) remain **open** and stack-dependent (OD-2 / OD-5); the open-decision registry is annotated, not struck.
- Until those gates land, a misbehaving agent is caught by **review**, not by mechanism — acceptable at bootstrap, revisited when OD-2 / OD-5 / OD-7 close.
- The model assumes the widened 6-state board; until that chore lands, the 3-state mapping in [`docs/roles/README.md`](../roles/README.md) applies.
- The board's **claim (assignee + status)** is the concurrency primitive that makes looped + parallel agents collision-safe **without** branch protection. Its precision improves once the board is widened (the Ready gate) and `blocked-by` / `area:*` labels exist.
- True in-session parallelism needs per-worker git worktrees; an external multi-process launcher (`scripts/run-fleet.ps1`) is a tracked follow-up, deferred until feature issues exist and the board is widened.
- Complements [ADR-0001](0001-record-architecture-decisions.md); supersedes nothing. Recorded *because* it is costly to reverse and not self-evident from the tree.
