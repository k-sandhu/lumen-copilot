# WORK_TRACKING.md — cold-start handoff

What a fresh agent session reads **after** [AGENTS.md](../AGENTS.md). It tells you where work lives, how to claim it, and how to turn user stories into issues.

*Last reviewed: 2026-06-17.*

---

## Roles & the operating model
Work is executed by a small fleet of single-purpose agents that hand off through the board: a **Planner** creates Ready issues, **Implementers** build them in parallel (one branch + worktree each), a single **Reviewer** merges, and **QA** tests merged `main` and files bugs. Each role has a harness-agnostic contract in **[`docs/roles/`](roles/README.md)**, invokable as `/planner`, `/implementer`, `/reviewer`, `/qa` in Claude Code, OpenCode, and Codex. The numbered workflow below is the **Implementer's** path; the other roles' protocols live in their contracts. Decision recorded in [ADR-0002](architecture/0002-multi-harness-agent-roles.md).

## Where work is tracked
- **Source of truth: the GitHub Projects board "Lumen Copilot"** — *not* a markdown table (tables drift). Status field: **Todo / In Progress / Done**.
- Issues live in the `k-sandhu/lumen-copilot` repo; each traces to a user story (or is a `type:chore` / `type:bug` / `type:adr` / `type:docs` item).
- Board: **[#7 — Lumen Copilot](https://github.com/users/k-sandhu/projects/7)** (Status: Todo / In Progress / Done).

## Branch model
- `main` advances **only** by merging a PR with `Closes #<N>` — no direct commits to `main`.
- One short-lived branch per issue: `feat/<#>-slug`, `fix/<#>-slug`, `docs/<#>-slug`, `chore/<#>-slug`.
- Board item → **Done** when the PR merges.

## The numbered workflow
1. Pick an issue from **Todo**; assign yourself; move it to **In Progress** (*claim before editing*).
2. Read `AGENTS.md` + the issue + any linked spec/ADR.
3. If behavior is unspecified → write a spec / open an ADR and **confirm** (precedence §4) before implementing.
4. *(once a test runner exists)* write the failing test first.
5. Smallest implementation to green.
6. *(once gates exist)* run `/verify`; for endpoint/data/auth/UI changes run `/verify-live` (round-trip + teardown).
7. Update affected docs **in the same change**.
8. Conventional-commit → push → open PR with `Closes #<N>`.
9. On merge, board item → **Done**.

## Status definitions
| Status | Meaning |
|---|---|
| Todo | Ready, unclaimed. |
| In Progress | Claimed (assignee set). |
| Done | Merged. |
| `blocked` (label) | Waiting on an open decision or dependency — comment why. |

---

## The story → issue pipeline (the "user-stories-first" path)

**Goal:** turn `glean-user-stories.md` (being finalized by a parallel effort) into **one issue per story**, grouped by epic (milestone) and tagged by persona, all on the board.

### Phase 1 — done now (mechanism in place)
- **Issue forms:** [`.github/ISSUE_TEMPLATE/user-story.yml`](../.github/ISSUE_TEMPLATE/user-story.yml), [`epic.yml`](../.github/ISSUE_TEMPLATE/epic.yml).
- **Base labels + board:** `powershell -File .\scripts\setup-board-and-labels.ps1` (idempotent).
- **Generator:** [`scripts/stories-to-issues.ps1`](../scripts/stories-to-issues.ps1) — idempotent, **dry-run by default**.

### Phase 2 — after the user stories settle (you trigger this)
```powershell
# 1. Preview — writes nothing (dry run is the default):
powershell -File .\scripts\stories-to-issues.ps1

# 2. Read the printed plan (titles, labels, milestones, what already exists).

# 3. Create for real:
powershell -File .\scripts\stories-to-issues.ps1 -Execute
```
> Use `-StoriesFile <path>` if the finalized stories live in a different file, and `powershell` (Windows PowerShell 5.1, present on this machine) or `pwsh` (PowerShell 7+, if installed) — the scripts target both.

**What the generator does per story:** creates a `[E?-?] <capability>` issue with the story + AC + feature in the body; labels it `type:user-story` + `epic:<n>` + `persona:<tag>`; attaches the `EPIC <n>` milestone; adds it to the board. It is **idempotent** — re-running skips any story whose issue already exists (matched by the `[E?-?]` title prefix), so you can safely run it again after the stories change.

> Epic milestones and `epic:*` / `persona:*` labels are created **at run time from the file**, not hardcoded — so they stay correct no matter how the stories are reorganized before they settle.

---

## Tool-failure fallbacks
- **`gh` not authenticated:** `gh auth login` (needs `repo` + `project` scopes). If `gh` is unavailable, create issues by hand from the template and **note the skipped automation** in your report.
- **`gh project` errors:** confirm the token has the `project` scope; otherwise skip the board step and note it.
- **No Docker / no stack yet:** expected — there is no stack to run. Don't fabricate one (🔓 open decision OD-2/OD-5).
- Always **note any skipped gate** in your final report.

## Parallel-agent isolation
- Multiple agents: one issue each, distinct branches, **claim on the board before editing** to avoid collisions.
- `glean-user-stories.md` is owned by a parallel effort right now — **don't edit it** unless that's your assigned task.
- *(once a stack exists)* isolate runtime via distinct compose project names + non-overlapping ports + unique test-data prefixes.

## Final-report discipline
Every handoff ends with: **what changed, which gates ran (and results), what was exercised, what was deferred and its residual risk.**
