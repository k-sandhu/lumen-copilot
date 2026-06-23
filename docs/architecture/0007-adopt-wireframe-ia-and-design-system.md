# 7. Adopt the wireframe IA & design system as the production UI target

- **Status:** Accepted
- **Date:** 2026-06-22
- **Builds on:** [ADR-0003](0003-application-stack.md) (stack), [ADR-0004](0004-architecture-boundaries-and-adapters.md) (boundaries), [ADR-0006](0006-contract-first-parallel-implementation.md) (contract-first parallel build)
- **Grounds in:** [spec 0003 — product scope & mission](../specs/0003-product-scope-and-mission.md), [AGENTS.md §2](../../AGENTS.md), the clickable wireframes in [docs/wireframes/](../wireframes/) and their [DESIGN.md](../wireframes/DESIGN.md)

## Context

The clickable wireframes merged at [`docs/wireframes/`](../wireframes/) (#77) walk the full MVP as seven surfaces — **Assistant, Search, Documents, Sources, Audit log, Admin, Sign in** — plus a design system (token model, 7 themes × light/dark, appearance panel, command palette, component kit). DESIGN.md is explicit that this is *"design exploration… input for the production UI, not a closed decision."*

Per [AGENTS.md §4/§8](../../AGENTS.md), adopting this as the production direction is a decision to **record before building**, not to default into. The gap today (verified against the live frontend routes and the frozen contract):

| Surface | Frontend today | Backend today | Gap |
|---|---|---|---|
| Sign in | ✅ `features/auth` | ✅ `/auth/*` | — |
| Assistant (chat) | ✅ `features/chat` | ✅ `/chat/*`, retrieval, citations | polish to the wireframe trust signals |
| Documents | ✅ `features/documents` | ✅ `/collections`, `/documents/*` | ingest-status + viewer polish |
| **Search** | ❌ | ⚠️ retrieval engine exists (#45); **no `/search` API** | new endpoint + screen |
| **Sources** | ❌ | ❌ no connector framework | #20/#27 (deferred) + endpoint + screen |
| **Audit log** | ❌ | ⚠️ audit *sink* exists (#23); **no query API** | new read API + screen |
| **Admin** | ❌ | ⚠️ partial (`require_roles`, `/models`) | governance/members/risk-tier endpoints + screen |

So three surfaces exist and run live; four need a **new backend endpoint** (not just UI) the frozen contract does not yet expose. The hard machinery for two of them — the hybrid retrieval engine and the audit sink — already exists; only the user-facing API surface and the screen are missing.

## Decision

1. **Adopt the wireframe information architecture** (the seven surfaces and their trust-signal patterns — citation chip + source inspector, permission pill, freshness pill, retrieval trace, audit provenance drawer, risk-tier badges) as the **production UI target** for `frontend/`. The wireframes remain the reference; `frontend/` is the implementation.

2. **Adopt the wireframe design system into `frontend/`** as shared UI infrastructure (token model, component kit, command palette). `frontend/` already uses a matching RGB-triple token approach, so this is a *port/reconcile*, not a rewrite.

   **Appearance scope for v1 — DECIDED (curated, sponsor-confirmed 2026-06-22):** ship the **single default theme (Aurora) + light / dark / system mode + the command palette** now. **Defer** the full 7-theme picker, the accent override, and the density / font axes to a later milestone. Rationale: the trust signals (citation chip, permission/freshness pills, retrieval trace, audit drawer, risk-tier badges) are load-bearing for the mission and ship now; the multi-theme breadth is polish. The token architecture still lands in full (theme×mode token sets, derived accents) so the deferred axes are additive later — only the *UI to switch them* is out of scope for v1.

3. **Each new surface is a contract-first vertical slice** ([ADR-0006](0006-contract-first-parallel-implementation.md)): the new endpoint lands in `contracts/openapi.yaml` first and is frozen, then BE and FE build against it in parallel. New wire shapes required: `GET /search`, `GET /audit` (filter + pagination), and read-mostly `/admin/*` (members/roles, model governance, risk tiers).

4. **Scope fences for v1, honoring the mission filters:**
   - **Admin** is **read-mostly** for v1 (view members/roles, model governance, risk-tier map). Write/governance *actions* are gated behind mission filter #3 (read-before-write) and the OD-4 risk tiers — deferred unless separately approved.
   - **Sources** depends on the connector framework (#20/#27), which is **deferred**. The Sources screen is sequenced **after** that lands; it is not in the first parallel wave.
   - **Search** and **Audit** reuse existing machinery (#45 retrieval, #23 audit sink) and lead the build.

5. **No new product behavior is invented here.** Anything the wireframes imply that isn't in [spec 0003](../specs/0003-product-scope-and-mission.md) or [spec 0004](../specs/0004-security-and-domain-invariants.md) (e.g. data-minimization toggles, approval workflows) is flagged in the issue that needs it and **confirmed before implementation**, per §8.

## Consequences

- The four missing surfaces become tracked, sequenced issues (see the M2 plan), each a clean contract→BE→FE slice that parallelizes per [ADR-0008](0008-conflict-free-parallel-delivery.md).
- The design-system port is a **wave-0 prerequisite**: it is shared FE infrastructure every new screen rides on, so it merges before the screens fan out.
- `contracts/openapi.yaml` gains `/search`, `/audit`, `/admin/*`; the contract is amended **once**, up front, and frozen for the wave.
- The wireframes stay as living reference; drift between them and `frontend/` is a reviewable defect, not silent.
- Deferred dependencies (connectors, write-side governance) are named here so they don't surprise the build.
