# Agent Operating Model — roles & how to invoke them

This repo is built by a small fleet of **single-purpose agents** that hand work to each other through the GitHub Projects board. The roles are **harness-agnostic**: the canonical contract for each lives here in `docs/roles/`, and every agent tool (Claude Code, OpenAI Codex, OpenCode, …) gets a thin wrapper that points back to it. **Define a role once; every harness inherits it.**

> All harnesses also read **[`AGENTS.md`](../../AGENTS.md)** (Claude Code via the `CLAUDE.md` pointer; Codex / OpenCode / others natively). These role contracts sit *on top of* that shared contract — they never override it.

## The roles

| Role | Cardinality | Owns | Contract |
|---|---|---|---|
| **Planner** | one | turns scope into Ready issues; triages bugs | [planner.md](planner.md) |
| **Implementer** | many (parallel) | builds one Ready issue → PR | [implementer.md](implementer.md) |
| **Orchestrator** | one per lane | fans an epic out into parallel Implementers; drives loops | [orchestrator.md](orchestrator.md) |
| **Reviewer / Merger** | one | reviews & merges PRs (sole path to `main`) | [reviewer.md](reviewer.md) |
| **QA** | one+ (continuous) | tests merged `main`, files bugs | [qa.md](qa.md) |

## The loop

```
   long-term goals / requirements
              │
        Planner (loop) ──files──▶ Backlog / Ready
            ▲                          │
            │ triage bugs              ▼
            │                  Orchestrator (loop) ──fans out──▶ Implementer × N
            │                                                    (parallel, isolated worktrees)
            │                                                          │ PR (In Review)
            │                                                          ▼
            │                                                   Reviewer (loop) ──merge──▶ main (Done)
            │                                                          │
            │ files type:bug                                           ▼
            └────────────────────────────  QA (loop) ◀──exercises── main
```

## The board is the message bus

Each status transition is a handoff between roles:

| Status | Set by | Meaning |
|---|---|---|
| Backlog | Planner / QA | exists, not yet Ready |
| Ready | Planner | passes the Definition of Ready — claimable |
| In Progress | Implementer | claimed, branch open |
| In Review | Implementer | PR open, awaiting Reviewer |
| Blocked | any | waiting on a dependency / open decision (comment why) |
| Done | Reviewer | merged |

> The board today ships with `Todo / In Progress / Done`. Until it's widened (a tracked chore — consolidated-structure §9/§12), map: **Backlog/Ready → Todo**, **In Review → In Progress**, **Done → Done**.

## Invoke a role

| Harness | Mechanism | Invoke |
|---|---|---|
| **Claude Code** | slash command + subagent | `/planner`, `/implementer`, `/reviewer`, `/qa` (or spawn the matching subagent in `.claude/agents/`) |
| **OpenCode** | command + agent | `/planner` … (agents in `.opencode/agent/`) |
| **OpenAI Codex** | custom prompt | `/planner` … (see Codex note below) |
| **Any other** | read the contract | point the agent at `docs/roles/<role>.md` + `AGENTS.md` |

`/orchestrator <epic | milestone | all>` fans an epic out into parallel Implementers; it runs as a **top-level command** (it spawns workers), not a subagent. See [Loops & parallelism](#loops--parallelism).

Each wrapper is intentionally thin: it tells the agent to **read `AGENTS.md` + `docs/roles/<role>.md` and execute that contract**. The behavior lives in the canonical doc, so updating a role is a one-file edit.

### Codex note
Codex discovers custom prompts from its global prompt dir (`~/.codex/prompts/`). The repo-local copies in [`.codex/prompts/`](../../.codex/prompts/) are the source of truth — copy them across so they appear as `/planner` etc.:

```bash
cp .codex/prompts/*.md ~/.codex/prompts/
```
```powershell
Copy-Item .codex\prompts\*.md $HOME\.codex\prompts\
```

Codex reads `AGENTS.md` automatically, so the shared contract is always in its context.

## Loops & parallelism

Two capabilities turn the roles into an autonomous fleet.

### Parallelism — fan an epic out
Point the **[Orchestrator](orchestrator.md)** at an epic, milestone, or "all Ready" and it launches **one Implementer per claimable item** in parallel — cross-cuttings first, one worker per `area:*`, up to a parallelism budget. Pointing `/implementer` at an epic delegates here automatically.
- **Claude Code / OpenCode:** the Orchestrator spawns Implementer subagents, each in its **own git worktree** (`isolation: worktree`), concurrently.
- **Any harness / CI:** launch one headless process per issue, each in its own worktree.

### Autonomy — run any role in a loop
Invoked with **no specific target**, each role queries the board for its next unit of work and does it, repeatedly:

| Role | Input horizon ("what it looks at") | Picks next | Stops when |
|---|---|---|---|
| Planner | long-term goals / requirements / open decisions / ungroomed epics | next area to groom → a Ready issue or a `type:spike` | the iteration's Ready backlog is deep enough, or an open decision needs a human |
| Orchestrator | an epic / iteration / "all Ready" | next claimable item(s) → dispatch workers | the target has no claimable items left |
| Implementer | open **Ready** items | top-priority Ready item, deps closed, area free | no claimable Ready item |
| Reviewer | open PRs (**In Review**) | oldest In Review PR (CI green, once OD-7) | the In Review queue is empty |
| QA | merged `main` since last pass | a Done feature not yet exercised | caught up to `main` |

**Run a loop:**
- **Claude Code:** `/loop /implementer` (self-paced) or `/loop 15m /reviewer` (interval); schedule recurring runs with the `schedule` skill.
- **Any harness:** an external `while` loop or cron calling the role command headlessly.

**Loop guardrails (every role obeys these):**
- **Pull, don't push** — one unit per iteration; an **empty queue is a clean stop**, not an error.
- **Claim is the mutex** — assign yourself + move the board status *before* working; if it's already claimed, skip it. This is what keeps looped + parallel agents collision-safe **without** branch protection.
- **Respect WIP** — a loop processes items *one at a time per agent*; parallelism comes from the **Orchestrator**, never from one agent grabbing many.
- **Never default an open decision** — file a spike / mark **Blocked** / surface it; loops must not guess.
- **Idempotent re-entry** — don't re-create, re-claim, or re-file what already exists.
- **Bounded** — honor a max-iteration / time / token budget, then report and stop.

**The autonomous pipeline:** a looped Planner feeds the backlog, a looped Orchestrator keeps Implementers saturated, a looped Reviewer drains PRs, and a looped QA watches `main` — four loops, one self-propelling pipeline that idles safely when there's nothing to do and stops to ask whenever an open decision blocks the way.

## Enforcement (today)

We are **not** using strict branch protection yet — role separation is **instruction-enforced** by these contracts, with two mechanical backstops where the harness allows:
- the **Reviewer** and **QA** subagents are declared **without Write/Edit** tools, so they *cannot* implement or patch — only review/merge and file bugs;
- **one branch + worktree per issue** keeps parallel Implementers from colliding.

Hardening this into real gates (branch protection, required CI checks, claim-enforcing hooks, auto-push) lands with **OD-6** (`.claude/` harness) and **OD-7** (CI) — see [open decisions](../specs/0001-open-decisions.md). The decision behind this structure is recorded in [ADR-0002](../architecture/0002-multi-harness-agent-roles.md).
