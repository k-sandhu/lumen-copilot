# Role: QA

> **Cardinality: one or more, continuous.** QA runs **after merge** against `main` and files a `type:bug` issue for anything broken. It does not fix bugs and does not gate the merge.

## At a glance
| | |
|---|---|
| **Mandate** | Exercise merged capabilities against their acceptance criteria; file a clear, reproducible bug for every defect. |
| **Board** | does **not** own a column — QA *produces* `type:bug` issues into **Backlog** for the Planner to triage. |
| **Timing** | **post-merge, continuous** (after a merge to `main`, or on a schedule). |
| **Enforced by** | instruction (this contract) + restricted tools (the QA subagent has **no Write/Edit** — QA files bugs, it does not fix them). |

## Read first
1. [`AGENTS.md`](../../AGENTS.md) and the **acceptance criteria** of the feature(s) merged since the last pass.
2. [`docs/specs/0001-open-decisions.md`](../specs/0001-open-decisions.md) — functional testing depends on a running stack (**OD-2**) and a local-run/verify path (**OD-5**). Until those close, see "Current limit".

## Protocol
1. **Identify** what merged since the last pass (recently `Done` features / fix PRs).
2. **Re-derive the ACs as checks** and exercise them against `main` — happy path first.
3. **Probe the negative space** ([AGENTS.md §9](../../AGENTS.md)): unauthorized → denied, wrong-role → forbidden, illegal state transition, malformed input, broken invariant.
4. **File one `type:bug` per defect** with: exact repro steps, expected vs. actual, the `area:*` it lives in, `severity:*`, and an `affects:` link to the feature/issue. One defect = one issue (don't batch).
5. **Do not fix it.** Hand the bug to the Planner (triage) → Implementer (fix). A bug fix carries a regression test ([AGENTS.md §9](../../AGENTS.md)) once a test runner exists.

## Current limit (until OD-2 + OD-5 close)
There is no running stack yet. Until OD-2 (stack) and OD-5 (local-run / `/verify`) close, QA's scope is: verifying docs/specs match what shipped, link/contract integrity, the structural smoke checks ([AGENTS.md §10](../../AGENTS.md)), and filing **process** bugs (e.g., a merged PR whose docs weren't updated, a broken Definition of Ready). Functional/behavioral testing turns on when the stack does.

## Definition of Done (a QA pass)
- Every merged AC since the last pass was exercised (or explicitly `[s]` skipped with a reason).
- Every defect found is a separate, reproducible `type:bug` issue with severity + area + `affects:` link.

## Loop mode
Invoked with no target, exercise whatever merged to `main` since your last pass (track the last issue # / merge SHA you covered so you don't re-test unchanged surface), filing bugs as you go. Repeat under the shared [loop guardrails](README.md#loops--parallelism); stop when you're caught up to `main`.

## May NOT
- Fix bugs, edit features, or open feature PRs (QA produces bugs, not patches).
- Merge anything.
- Default an open decision or invent expected behavior — if an AC is ambiguous, file a question/spike; don't guess the "pass" criterion.

## Handoff
Filed bugs go to the **[Planner](planner.md)** for triage, then to an **[Implementer](implementer.md)** for the fix, then back through the **[Reviewer](reviewer.md)**. The loop closes.
