# Spec 0001 — Open Decisions (parked, not defaulted)

> Playbook rule: *any "not sure" is recorded as an open decision — never silently defaulted.* This is that registry. Each row is **blocking** for the scaffolding that depends on it; an agent must not invent an answer (see `AGENTS.md` §4 precedence).

**Status:** open (OD-1 closed 2026-06-17). **Last reviewed:** 2026-06-17.

| # | Decision | Depends on | Blocks | Notes |
|---|---|---|---|---|
| ~~**OD-1**~~ | ~~**Product scope & mission adjectives**~~ | — | — | ✅ **Closed 2026-06-17** by [spec 0003](0003-product-scope-and-mission.md) + `AGENTS.md` §2. MVP = grounded chat over connected + uploaded docs; multi-tenant SaaS; filters: permissioned / cited / read-before-write / auditable. |
| **OD-2** | **Tech stack** (languages, frameworks, datastore, vector/search store) | OD-1 | `docker-compose.yml`, test tiers, smoke code-greps | Record via ADR when chosen. |
| **OD-3** | **Architecture boundaries & adapters** | OD-2 | `AGENTS.md` §6 table + adapter rules | "The only place that talks to X" per external system. |
| **OD-4** | **Security & domain invariants** | OD-1 | `AGENTS.md` §8 negative-test categories; smoke domain checks | Derive from real failure modes; each generates a negative test. |
| **OD-5** | **Local-run path** (compose + dev runner) | OD-2 | `AGENTS.md` §10 `/verify` & `/verify-live` mechanisms | Target: one `docker compose up`. |
| **OD-6** | **`.claude/` harness** (permission tiers, hooks, slash commands, review subagents) | OD-2 + smoke existing | smoke-on-change, auto-push, `/verify`, `/verify-live` | **Partially addressed:** slash-commands, role subagents, the Orchestrator (parallel fan-out), and loop-mode invocation landed cross-harness via [ADR-0002](../architecture/0002-multi-harness-agent-roles.md) (2026-06-17). Permission tiers, hooks, auto-push, `/verify`, and the external multi-process launcher still deferred to the post-stack phase. |
| **OD-7** | **CI** | smoke + tests exist | local/CI parity | Mirror the fast-gate chain exactly. |

## How to close a decision
1. Open an **ADR** (OD-2 / OD-3 / OD-5) or a **spec** (OD-1 / OD-4); get confirmation.
2. Fill the corresponding `AGENTS.md` section **in the same change**.
3. Strike the row here with the date + a link to the ADR/spec that closed it.

> Until OD-1 is closed, treat all product behavior as undefined: **ask, don't assume.**
