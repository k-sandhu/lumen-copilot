# Spec 0001 — Open Decisions (parked, not defaulted)

> Playbook rule: *any "not sure" is recorded as an open decision — never silently defaulted.* This is that registry. Each row is **blocking** for the scaffolding that depends on it; an agent must not invent an answer (see `AGENTS.md` §4 precedence).

**Status:** open — OD-1 / OD-2 / OD-3 / OD-4 / OD-5 closed. **OD-6 (partial), OD-7 remain open — don't default them; ask.** **Last reviewed:** 2026-06-18.

| # | Decision | Depends on | Blocks | Notes |
|---|---|---|---|---|
| ~~**OD-1**~~ | ~~**Product scope & mission adjectives**~~ | — | — | ✅ **Closed 2026-06-17** by [spec 0003](0003-product-scope-and-mission.md) + `AGENTS.md` §2. MVP = grounded chat over connected + uploaded docs; multi-tenant SaaS; filters: permissioned / cited / read-before-write / auditable. |
| ~~**OD-2**~~ | ~~**Tech stack**~~ | — | — | ✅ **Closed 2026-06-17** by [ADR-0003](../architecture/0003-application-stack.md) + `AGENTS.md` §3. FastAPI · React/Vite SPA · Postgres+pgvector · Redis · MinIO · Celery · LiteLLM/OpenRouter; OSS-only, LLM-agnostic. |
| ~~**OD-3**~~ | ~~**Architecture boundaries & adapters**~~ | — | — | ✅ **Closed 2026-06-17** by [ADR-0004](../architecture/0004-architecture-boundaries-and-adapters.md) + `AGENTS.md` §6 table ("the one module that owns each concern"). |
| ~~**OD-4**~~ | ~~**Security & domain invariants**~~ | — | — | ✅ **Closed 2026-06-18** by [spec 0004](0004-security-and-domain-invariants.md) + `AGENTS.md` §9. Row-level tenancy; deny-by-default ACL; app-managed authn (SSO→Keycloak); audit taxonomy; read-before-write tiers. Each invariant → a negative test (INV-1..INV-8). |
| ~~**OD-5**~~ | ~~**Local-run path**~~ (compose + dev runner) | — | — | ✅ **Closed 2026-06-17** by [ADR-0005](../architecture/0005-local-run-and-developer-workflow.md). One `docker compose up`; `471xx` host ports. The `/verify` gate **scripts** still pend OD-6/OD-7. |
| **OD-6** | **`.claude/` harness** (permission tiers, hooks, slash commands, review subagents) | OD-2 + smoke existing | smoke-on-change, auto-push, `/verify`, `/verify-live` | **Partially addressed:** slash-commands, role subagents, the Orchestrator (parallel fan-out), and loop-mode invocation landed cross-harness via [ADR-0002](../architecture/0002-multi-harness-agent-roles.md) (2026-06-17). Permission tiers, hooks, auto-push, `/verify`, and the external multi-process launcher still deferred to the post-stack phase. |
| **OD-7** | **CI** | smoke + tests exist | local/CI parity | Mirror the fast-gate chain exactly. |

## How to close a decision
1. Open an **ADR** (OD-2 / OD-3 / OD-5) or a **spec** (OD-1 / OD-4); get confirmation.
2. Fill the corresponding `AGENTS.md` section **in the same change**.
3. Strike the row here with the date + a link to the ADR/spec that closed it.

> Treat every **still-open** decision above (OD-6 remainder, OD-7) as undefined: **ask, don't assume.**
