# AGENTS.md — Lumen Copilot agent contract

> **Read this top to bottom before running or changing anything in this repo.**
> This is the single canonical contract for *any* coding agent (Claude Code, Codex, Cursor, …). Tool-specific mechanism (Claude Code hooks, permissions, slash commands) lives under `.claude/` and must never add a rule that isn't reflected here.
>
> **One principle governs everything: _prose ↔ mechanism._** Every rule here should be backed by an executable mechanism that survives a cold agent session — a smoke check, a hook, a test, a CI gate. A rule with nothing that fails when it's violated is not yet real. As the stack lands, every rule below earns its mechanism; until then, the rules are enforced by review and the Definition of Done.

**Repo status (2026-06-16): bootstrap.** The product scope, stack, and architecture are being defined from user stories (a parallel effort). Sections marked **🔓 OPEN DECISION** are deliberately unfilled and tracked in [docs/specs/0001-open-decisions.md](docs/specs/0001-open-decisions.md) — *do not invent answers for them; ask instead.*

---

## 1. Read order (cold start)
1. **This file** (`AGENTS.md`) — the contract.
2. [docs/WORK_TRACKING.md](docs/WORK_TRACKING.md) — how work is tracked, claimed, and handed off.
3. [docs/specs/0001-open-decisions.md](docs/specs/0001-open-decisions.md) — what is intentionally undecided (don't default it).
4. The **GitHub Projects board** (source of truth for status) and the **issue** you're working.
5. The spec/ADR for the area you're touching.

## 2. Mission — decision filters
🔓 **OPEN DECISION.** The mission adjectives — the short list that lets an agent make *novel* decisions aligned with intent — will be set once the product scope is defined from the user stories. Until then, when explicit rules don't cover a decision, **stop and ask** rather than invent product behavior. (See precedence, §4.)

## 3. Stack / architecture context
🔓 **OPEN DECISION.** No application stack has been chosen. Do **not** introduce frameworks, services, datastores, or a `docker-compose.yml` until the choice is recorded in an ADR. See [open decisions](docs/specs/0001-open-decisions.md) (OD-2).

## 4. Source-of-truth precedence
When sources conflict, resolve in this order:
1. **Explicit user instructions** (this session).
2. **Security & privacy invariants** (🔓 to be defined; until then default to least exposure and ask).
3. **Specs & ADRs** under `docs/`.
4. **This contract** (`AGENTS.md`).
5. **Existing code & tests.**

> If behavior isn't covered anywhere, **write the doc / open an ADR and confirm first — don't invent behavior** at the bottom of this list. *Implementation is never the source of truth.*

## 5. Self-modification gate
- Agents may **read** any contract/agent file freely and **propose** changes (in a PR or as a suggestion).
- Agents may **not edit** `AGENTS.md`, `CLAUDE.md`, or anything under `.claude/` without explicit human approval in the session.
- Architecture-level changes route to an **ADR** ([docs/architecture/](docs/architecture/)), never to edits buried in this contract.

## 6. Architecture boundaries
🔓 **OPEN DECISION.** The `directory/file → single responsibility` table and the adapter rules ("the *only* place that talks to X") will be filled when the stack lands (OD-3). The **principle** holds now: provider-/vendor-specific code stays behind one named module; don't reach for a vendor SDK when an HTTP boundary will do.

## 7. Hard rules (process — in force now)
Stack-independent; binding from the first commit:
1. **Every substantive change traces to a tracked issue.** No issue-less work. Discover a follow-up mid-task? File it as its *own* issue — don't widen the current one.
2. **Claim before editing** — assign yourself and move the board item to *In Progress* before you start.
3. **Branch per issue.** Never commit directly to `main`. `main` advances only by merging a PR whose body contains `Closes #<N>`.
4. **Conventional Commits**, one logical change per commit.
5. **Spec-first for behavior** (§8) and **test-first for code** (§9). These become hard gates the moment the stack exists; treat them as binding now in spirit.
6. **Living docs update in the same change** that alters what they describe — never a later "docs pass."
7. **Status markers carry accountability:** `[x]` done · `[ ]` todo · `[~]` partial · `[s]` skipped. Every `[~]`/`[s]` appends **date + reason + residual risk**. Never mark `[x]` without noting the verification that proves it.
8. **Don't edit contract/agent files** without approval (§5).
9. **Don't edit `glean-user-stories.md`** unless that is your assigned task — it is owned by a parallel effort.

## 8. Spec-driven (hard gate — binding once behavior exists)
1. Read the relevant doc/ADR before changing behavior.
2. If behavior isn't specified, **write the doc / open an ADR and confirm before implementing.**
3. Capture acceptance criteria before code; keep tests traceable to documented behavior.
4. Update the doc **in the same change** when behavior shifts.

## 9. Test-driven (hard gate — binding once a test runner exists)
Write the test → watch it fail for the *right reason* → smallest change to green → confirm green. Plus:
- A **regression test for every bug fix.**
- **Negative tests required** for: unauthorized → denied, wrong-role → forbidden, illegal state transition, malformed input, broken invariant. (The concrete categories firm up with the security invariants — §2/§6 open decisions.)

## 10. Verification — tiers & named gates
🔓 Mechanisms are **pending the stack** (OD-5). The intended shape (do not build yet):
- **Smoke** — dependency-light *structural* checks (files exist, contract links resolve, no `:latest`, env vars present, doc-freshness). Seconds. Runs in hooks + CI.
- **Unit** — per-service fast tests. Seconds.
- **Live** — real HTTP against the running stack, with **round-trip read-back** and **teardown**. Minutes.
- Named gates `/verify` (smoke → unit → compose config) and `/verify-live` will wrap these; CI will run the identical fast-gate chain (local/CI parity).

## 11. Git & scope
- Short-lived branch (or worktree) **per issue**; merge fast via PR.
- Conventional Commits; one logical change per commit. (Auto-push-on-commit arrives with the `.claude/` harness so nothing is stranded.)
- Keep each PR scoped to one issue; body carries `Closes #<N>`.

## 12. Repo layout (current)
```
.
├── AGENTS.md                  # this contract (canonical)
├── CLAUDE.md                  # → AGENTS.md (symlink, or pointer file on Windows)
├── README.md
├── glean-user-stories.md      # input research → source for the issue pipeline (being finalized)
├── .github/ISSUE_TEMPLATE/    # user-story + epic issue forms
├── docs/
│   ├── WORK_TRACKING.md        # cold-start handoff + the story→issue pipeline
│   ├── specs/                  # specifications (0001 = open decisions)
│   └── architecture/           # ADRs (0001 = record ADRs)
└── scripts/
    ├── setup-board-and-labels.ps1   # idempotent: base labels + Projects board
    └── stories-to-issues.ps1        # idempotent, dry-run default: stories → issues
```
*Grows as the stack lands: `docker-compose.yml`, services, `tests/`, the `.claude/` harness, CI.*

## 13. Common commands
```powershell
# Inspect tracking
gh project list --owner "@me"
gh issue list

# Issue pipeline (details in docs/WORK_TRACKING.md)
powershell -File .\scripts\setup-board-and-labels.ps1      # base labels + board (idempotent)
powershell -File .\scripts\stories-to-issues.ps1           # preview the issues (DEFAULT: dry run)
powershell -File .\scripts\stories-to-issues.ps1 -Execute  # create them (only after stories settle)

# Daily git
git switch -c feat/<issue#>-<slug>
git commit -m "feat: summary"        # Conventional Commits
gh pr create --fill                   # body must contain: Closes #<N>
```

## 14. Operational gotchas
- **Windows symlinks:** `CLAUDE.md` is a symlink to `AGENTS.md` where the OS permits; otherwise it's a one-line pointer file. Either way **edit only `AGENTS.md`.**
- **Issue creation is gated on the user stories settling.** `stories-to-issues.ps1` defaults to dry-run precisely so a half-written story set isn't turned into real issues.

## 15. Definition of Done (current phase)
A change is done only when:
- [ ] It traces to a GitHub issue; the board item is updated.
- [ ] It's on a branch; a PR is open with `Closes #<N>`; the branch is pushed.
- [ ] Commits are Conventional, one logical change each.
- [ ] Any doc/ADR the change affects is updated in the **same** PR.
- [ ] `[~]`/`[s]` status markers (if any) carry date + reason + residual risk.
- [ ] A final report is delivered (what changed, what ran, what was deferred + risk).
- [ ] *(binds once the stack exists)* spec/ADR exists & matches; test written first; `/verify` green; scoped `/verify-live` passed with round-trip + teardown.

## 16. Review checklist (self-audit before finishing)
- [ ] Did I claim an issue and branch **before** editing?
- [ ] Is everything I changed in scope for that one issue?
- [ ] Did I touch a contract/agent file without approval? *(should be no)*
- [ ] Did I invent product behavior for an 🔓 open decision? *(should be no — I asked)*
- [ ] Are the docs updated in the same change?
- [ ] Is there a `Closes #<N>` and a final report?

## 17. Where to look
| Concern | Owner doc |
|---|---|
| How work is tracked / claimed / handed off | [docs/WORK_TRACKING.md](docs/WORK_TRACKING.md) |
| What's intentionally undecided | [docs/specs/0001-open-decisions.md](docs/specs/0001-open-decisions.md) |
| Why a hard-to-reverse choice was made | [docs/architecture/](docs/architecture/) (ADRs) |
| Turning user stories into issues | [docs/WORK_TRACKING.md](docs/WORK_TRACKING.md) + `scripts/stories-to-issues.ps1` |
| Tool-specific automation | `.claude/` *(pending — OD-6)* |

---
*Last reviewed: 2026-06-16. This contract is product-agnostic by design while the product scope is defined from the user stories.*
