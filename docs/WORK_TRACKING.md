# WORK_TRACKING.md — cold-start handoff

What a fresh agent session reads **after** [AGENTS.md](../AGENTS.md). It tells you where work lives, how to claim it, and how work is structured.

*Last reviewed: 2026-07-02.*

---

## Roles & the operating model
Work is executed by a small fleet of single-purpose agents that hand off through the board: a **Planner** creates Ready issues, **Implementers** build them in parallel (one branch + worktree each), a single **Reviewer** merges, and **QA** tests merged `main` and files bugs. Each role has a harness-agnostic contract in **[`docs/roles/`](roles/README.md)**, invokable as `/planner`, `/implementer`, `/reviewer`, `/qa` in Claude Code, OpenCode, and Codex. The numbered workflow below is the **Implementer's** path; the other roles' protocols live in their contracts. Decision recorded in [ADR-0002](architecture/0002-multi-harness-agent-roles.md).

## Where work is tracked
- **Source of truth: the GitHub Projects board "Lumen Copilot"** — *not* a markdown table (tables drift). Status field: **Backlog / Ready / In Progress / In Review / Blocked / Done**.
- Issues live in the `k-sandhu/lumen-copilot` repo. The **unit of claim is the Feature** (`type:feature`), not the user story — stories are traceability (the *why*). Foundations are `type:cross-cutting`; investigations that end in an ADR are `type:spike`; plus `type:epic` / `type:chore` / `type:bug` / `type:adr` / `type:docs`. This layer model is [spec 0002](specs/0002-feature-issue-structure.md), reconciled in [consolidated-structure.md](product/consolidated-structure.md) §2; the Planner cuts these by hand ([docs/roles/planner.md](roles/planner.md)) — see the pipeline note below.
- Board: **[#7 — Lumen Copilot](https://github.com/users/k-sandhu/projects/7)** (Status: Backlog / Ready / In Progress / In Review / Blocked / Done).

## Milestones (delivery waves — the board's cadence axis)
Milestones are **thematic waves**, not the numbered iterations spec 0002 §7.3 originally sketched. Current map:

| Milestone | State | What it covers |
|---|---|---|
| M0 Foundations · M1 Grounded Chat v0 · M2 Trust Surfaces | **done** | Tenancy, permissioned retrieval, upload+ingest, grounded chat with citations, search, audit, admin, connector framework + web source. |
| **M2.5 Stabilization** | **active** | Hardening of the M0–M2 QA waves + the ADR-0010 OpenSearch retrieval-store cutover ([#189](https://github.com/k-sandhu/lumen-copilot/issues/189)). Umbrella: [#288](https://github.com/k-sandhu/lumen-copilot/issues/288). Runs **in parallel** with M3. |
| **M3 Agents & Extensibility** · **M4 Agent Governance & Autonomy** | **active** | The committed agents/extensibility program ([#196](https://github.com/k-sandhu/lumen-copilot/issues/196)): custom assistants, tool platform, MCP, sandbox, scheduled runs. |
| **M5 Connector Breadth** | **active** | The committed connector-breadth program ([#289](https://github.com/k-sandhu/lumen-copilot/issues/289), sponsor commitment 2026-07-18, spec 0003 §4.2): connector SDK + OAuth + ACL mirroring ([ADR-0019](architecture/0019-connector-sdk-and-oauth.md)); first managed connector Google Drive. Lane: `area:connectors` — disjoint from M2.5/M3/M4. |
| *(gated next-wave epics, no milestone yet)* | **Backlog** | Knowledge trust, collaboration/trust, actions & approvals, proactive, research & artifacts, AgentOps — each awaits a sponsor commitment before it earns a milestone (spec 0003 §4 pattern). |

M2.5 and M3/M4 are **disjoint lanes** — a stabilization team and an agents team run concurrently without shared files. The one shared seam was [#192](https://github.com/k-sandhu/lumen-copilot/issues/192)↔[#207](https://github.com/k-sandhu/lumen-copilot/issues/207) (retrieval ↔ tool registry); [#207](https://github.com/k-sandhu/lumen-copilot/issues/207) has since merged, so [#192](https://github.com/k-sandhu/lumen-copilot/issues/192) now lands against the settled registry and the lanes are effectively fully disjoint.

## Branch model
- `main` advances **only** by merging a PR with `Closes #<N>` — no direct commits to `main`.
- One short-lived branch per issue: `feat/<#>-slug`, `fix/<#>-slug`, `docs/<#>-slug`, `chore/<#>-slug`.
- Board item → **Done** when the PR merges.

## The numbered workflow
1. Pick an issue from **Ready**; assign yourself; move it to **In Progress** (*claim before editing*).
2. Read `AGENTS.md` + the issue + any linked spec/ADR.
3. If behavior is unspecified → write a spec / open an ADR and **confirm** (precedence §4) before implementing.
4. *(once a test runner exists)* write the failing test first.
5. Smallest implementation to green.
6. *(once gates exist)* run `/verify`; for endpoint/data/auth/UI changes run `/verify-live` (round-trip + teardown).
7. Update affected docs **in the same change**.
8. Conventional-commit → push → open PR with `Closes #<N>`.
9. On merge, board item → **Done**.

## Status definitions
| Status | Meaning | Claimable? |
|---|---|---|
| Backlog | Exists, not yet Ready (missing AC, scope fences, or deps). | no |
| Ready | Passes the Definition of Ready. | **yes** |
| In Progress | Claimed (assignee set), branch open. | (claimed) |
| In Review | PR open, awaiting the Reviewer. | (claimed) |
| Blocked | Waiting on an open decision / dependency — comment why. | no |
| Done | Merged. | n/a |

---

## How work gets created (the operating model)

**The Planner cuts issues by hand** from the groomed backlog ([docs/roles/planner.md](roles/planner.md)); the layer model is [spec 0002](specs/0002-feature-issue-structure.md). A Feature satisfies 3–7 stories, owns one `area:*`, and is claimable only when it passes the **Definition of Ready** (capability paragraph · ≥1 testable AC incl. a negative · IN/OUT scope fences · dependencies listed · `area:`+`size:` · milestone or "unassigned" · no `needs-spec`/`needs-adr` · no open `blocked-by`). Cross-cuttings are filed and Ready **before** the Features that depend on them; Spikes close an open decision into an ADR. Dependency edges (`blocked-by:`/`blocks:`) live in the issue body and gate the board.

> **Why not a bulk story→issue generator?** The `stories-to-issues.ps1` path below exists and works, but the fleet **did not** mass-generate one-issue-per-story: that model conflates the unit of *traceability* (the 136+ stories) with the unit of *claim* (~30–50 Features), and it can't surface the invisible foundations (auth, tenancy, ingestion, the tool registry…) that aren't user stories at all. See [spec 0002](specs/0002-feature-issue-structure.md) §1 and [consolidated-structure.md](product/consolidated-structure.md) §2 for the reasoning. The generator remains useful for **seeding a fresh epic's stories** as traceability stubs; treat it as a helper, not the pipeline.

### The generator (available helper, not the default path)
- **Issue forms:** [`.github/ISSUE_TEMPLATE/user-story.yml`](../.github/ISSUE_TEMPLATE/user-story.yml), [`epic.yml`](../.github/ISSUE_TEMPLATE/epic.yml).
- **Base labels + board:** `powershell -File .\scripts\setup-board-and-labels.ps1` (idempotent).
- **Generator:** [`scripts/stories-to-issues.ps1`](../scripts/stories-to-issues.ps1) — idempotent, **dry-run by default**, canonical source `docs/product/user-stories.md`.

```powershell
# Preview (writes nothing — dry run is the default):
powershell -File .\scripts\stories-to-issues.ps1 -StoriesFile docs/product/user-stories.md
# Create for real (only after a human confirms the slice):
powershell -File .\scripts\stories-to-issues.ps1 -StoriesFile docs/product/user-stories.md -Execute
```
> Use `powershell` (Windows PowerShell 5.1) or `pwsh` (PowerShell 7+). **Known gotcha** ([consolidated-structure.md](product/consolidated-structure.md) §11): under Windows PowerShell 5.1 the script must be UTF-8 **with BOM** or the em-dash in the `# EPIC n —` heading regex breaks and milestones preview as bare `EPIC`. It is idempotent — re-running skips any story whose `[E?-?]` issue already exists.

---

## Tool-failure fallbacks
- **`gh` not authenticated:** `gh auth login` (needs `repo` + `project` scopes). If `gh` is unavailable, create issues by hand from the template and **note the skipped automation** in your report.
- **`gh project` errors:** confirm the token has the `project` scope; otherwise skip the board step and note it.
- **No Docker / no stack yet:** expected — there is no stack to run. Don't fabricate one (🔓 open decision OD-2/OD-5).
- Always **note any skipped gate** in your final report.

## Parallel-agent isolation
- Multiple agents: one issue each, distinct branches, **claim on the board before editing** to avoid collisions.
- WIP cap (spec 0002 §8): one agent = at most **1 Feature + 1 Cross-cutting**; two agents don't own the same `area:*` unless the dependency edge is explicit. Cross-cuttings are claimed **before** their dependents — the single largest parallelization unlock.
- `glean-user-stories.md` remains **read-only** for agents ([AGENTS.md](../AGENTS.md) §7.9) — the vendor-neutral rewrite it fed (`docs/product/user-stories.md`) is the working corpus.
- Isolate runtime via distinct compose project names + non-overlapping ports (`471xx`, [ADR-0005](architecture/0005-local-run-and-developer-workflow.md)) + unique test-data prefixes.

## Final-report discipline
Every handoff ends with: **what changed, which gates ran (and results), what was exercised, what was deferred and its residual risk.**
