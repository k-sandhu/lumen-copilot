# Spec 0003 — Product Scope & Mission (closes OD-1)

> Closes **OD-1** in [0001-open-decisions.md](0001-open-decisions.md). Decided with the human sponsor in session on **2026-06-17**. This spec defines *what Lumen Copilot is*, the *first buildable scope*, and the *decision-filters* that let an agent make novel calls aligned with intent (it fills [AGENTS.md](../../AGENTS.md) §2).

**Status:** adopted. **Last reviewed:** 2026-07-18 *(§4.2 records the [#289](https://github.com/k-sandhu/lumen-copilot/issues/289) committed connector-breadth scope expansion; §4.1 recorded [#196](https://github.com/k-sandhu/lumen-copilot/issues/196) E6/E7/E15; mission filters and invariants unchanged)*. **Tracking issue:** [#11](https://github.com/k-sandhu/lumen-copilot/issues/11).

---

## 1. Mission

**Lumen Copilot is a multi-tenant enterprise Work-AI assistant** — a *grounded chat assistant* that answers, summarizes, and drafts over each user's **connected enterprise sources** and **uploaded documents**, where every answer is **permissioned, cited, and auditable**.

It does not replace the systems where work lives; it sits across them, retrieves only what the asking user is already allowed to see, and grounds every response in verifiable sources.

## 2. Decision-filters (the mission adjectives — AGENTS.md §2)

When explicit rules don't cover a decision, choose the option that best satisfies these — and when they still don't resolve it, **stop and ask**:

1. **Permissioned by default.** Never surface or act on data the requesting user can't already access. Enforce at *retrieval* time and *action* time, mirroring source ACLs — not as an afterthought.
2. **Citation-backed.** Every answer traces to a verifiable source passage. Prefer "I don't know / I couldn't find it" over a confident unsourced claim.
3. **Read before write.** Read-only value ships first. Any consequential or write action is gated behind explicit human approval and a stated risk tier; nothing writes silently.
4. **Auditable.** Every retrieval, answer, and action emits an audit event. Trust is provable after the fact, not assumed.

These are ordered by precedence when two filters tension (permission wins over helpfulness; a cited "no" beats an uncited "yes").

## 3. Scope — the first buildable product (M0–M1)

The MVP spine is **grounded chat over connected + uploaded content**. Concretely **IN**:

| In scope (MVP) | Epic / cross-cutting | Why it's load-bearing now |
|---|---|---|
| Multi-tenant SaaS with hard tenant isolation | CC-2 | Deployment decision: many orgs on shared infra. |
| Identity: SSO + per-user identity | CC-3 | Multi-tenant auth; "who is asking" gates retrieval. |
| Connector framework + ingestion skeleton (managed + push) | E1 / CC-4, CC-5 | Get connected enterprise content in, with ACLs mirrored. |
| **Document upload of many file types** → ingest → retrieve | E1 / CC-5, CC-12 | Core sponsor requirement: chat over user-uploaded docs, not only connectors. |
| Permission-aware retrieval | CC-1 | Filter #1 — permissioned by default. |
| Grounded chat assistant: ask, summarize, draft over retrieved + uploaded context | E3 / CC-6, CC-9 | The product surface itself. |
| Passage-level citations & provenance | CC-11 | Filter #2 — citation-backed. |
| Audit log of retrieval/answer events | CC-8 | Filter #4 — auditable. |

## 4. Out of scope (roadmap — explicitly NOT in the MVP)

Deferred to later iterations; named here so agents don't silently pull them in (file a sibling issue, never widen):

- **Write-back / consequential actions** into external systems (E5) — beyond *read before write*; any write lands later, behind approval (CC-7).
- **Computer use & browser/desktop automation** (E16).
- **Proactive work intelligence** (E4) and **meetings/communication intelligence** (E10).
- **Departmental automation packs** (E12).
- **Deep research & artifact-generation suites** (E8, E9) beyond in-chat summarize/draft.
- **Advanced governance & AgentOps** (E14) beyond the M0 audit log.

The full 16-epic vision in [consolidated-structure.md](../product/consolidated-structure.md) remains the **roadmap**; this spec sets only the *first* demoable scope.

### 4.1 Committed scope expansion — agents & extensibility (program epic [#196](https://github.com/k-sandhu/lumen-copilot/issues/196))

**Update (2026-07-02, M3).** The sponsor has committed a **defined slice** of the roadmap epics **E6 (Agent Builder / reusable skills)**, **E7 (Autonomous & scheduled agents)**, and **E15 (Developer Platform, incl. sandboxed code execution)** — previously listed above as out of the MVP. The committed slice is the [#196](https://github.com/k-sandhu/lumen-copilot/issues/196) program:

- **Custom assistants & agent builder** — define, configure, version, and share assistants; pick their knowledge scope + tools (E6).
- **Agent tool platform** — a governed tool registry (CC-7) with two new tools: **internet / web search** and **file-writing → artifacts** (part E15, part E2 tooling).
- **MCP server integration** — per-tenant Model Context Protocol servers whose tools enter the same governed registry (E15 extensibility).
- **Code-execution sandbox** — agent-authored Python run in an isolated sandbox (E15-7).
- **Scheduled & headless agent runs** — run an assistant on a schedule without a user present (E7).

**This slice is governed by the *unchanged* mission filters (§2) and the existing risk tiers ([spec 0004](0004-security-and-domain-invariants.md) §2.5) — this update records scope only and changes no filter and no invariant.** Concretely: retrieval inside a headless run still enforces the §2 *permissioned* filter as the run's owner; agent **file-writing to the app's own tenant-scoped storage is T1** (owner-gated, audited, no extra approval); web search and MCP/sandbox egress ride the existing deny-by-default egress stance; and every new tool declares its tier.

**Architecture record.** The five subsystems' costly, non-obvious choices are decided in the [#196](https://github.com/k-sandhu/lumen-copilot/issues/196) spikes — agent runtime ([#202](https://github.com/k-sandhu/lumen-copilot/issues/202)), MCP transport/boundary/egress ([#203](https://github.com/k-sandhu/lumen-copilot/issues/203)), sandbox technology ([#204](https://github.com/k-sandhu/lumen-copilot/issues/204)), web-search provider/egress ([#205](https://github.com/k-sandhu/lumen-copilot/issues/205)), and scheduling/headless-run design ([#206](https://github.com/k-sandhu/lumen-copilot/issues/206)). Each closes to its own ADR (the next sequential slots, **ADR-0011 … ADR-0015**, per [architecture/README.md](../architecture/README.md)); each new external system (MCP, sandbox, search provider) gets **one owning module + a boundary-table row** in the ADR that introduces it ([ADR-0004](../architecture/0004-architecture-boundaries-and-adapters.md) §6). Link the ADRs from here as they land.

**What stays out** (the remainder of E6/E7/E15 — file a sibling issue, never widen): **T2+ external write-back / consequential actions** (E5/CC-7, still approval-gated — see §4 above and [spec 0004](0004-security-and-domain-invariants.md) §2.5); **event-driven / webhook triggers** (E7-2) beyond scheduled runs; **computer use / browser automation** (E16); and **third-party OAuth connectors** *(committed later — §4.2)*.

### 4.2 Committed scope expansion — connector breadth (epic [#289](https://github.com/k-sandhu/lumen-copilot/issues/289))

**Update (2026-07-18).** The sponsor has committed **Connector Breadth v1** (epic [#289](https://github.com/k-sandhu/lumen-copilot/issues/289)) — the slice of roadmap **E1** that §4.1 explicitly left out as "third-party OAuth connectors, routed via their own ADRs". The committed slice:

- **Managed OAuth connectors, read-only** — OAuth 2.0 per tenant-admin, tokens in the CC-C secrets vault ([#209](https://github.com/k-sandhu/lumen-copilot/issues/209)) (E1-1).
- **Source-ACL mirroring** — the [spec 0004](0004-security-and-domain-invariants.md) §2.2 mirrored principal-set model, implemented and negative-test-proofed per connector (E1-2), with sync/ACL freshness surfaced to users (E1-3).
- **Incremental sync** — cursor-based change polling through the existing sync pipeline.
- **Connector SDK + conformance kit** — the documented capability protocols a next connector implements (E1-5).
- **First managed connector: Google Drive** (sponsor decision, recorded on [#289](https://github.com/k-sandhu/lumen-copilot/issues/289)).

**Architecture record:** [ADR-0019](../architecture/0019-connector-sdk-and-oauth.md) (from spike [#290](https://github.com/k-sandhu/lumen-copilot/issues/290)) decides the OAuth flow, the concrete ACL-mirror contract, change detection, and the SDK shape. **This slice is governed by the *unchanged* mission filters (§2) and risk tiers ([spec 0004](0004-security-and-domain-invariants.md) §2.5)** — read-only connectors are T0; the OAuth connect/consent is an audited admin action; deny-by-default is preserved (unmapped or stale ACLs deny).

**What stays out** (file a sibling issue, never widen): **write-back into sources** (E5, approval-gated); **webhook/event-driven sync** (E7-2); **third-party / push / SDK-only custom connectors** (isolation must be revisited first — ADR-0019 §4); **SCIM / group expansion** (CC-3 v2 — Google-group ACLs deny until it lands).

## 5. Personas

Primary (MVP — the people who use or operate the assistant):

| Tag | Persona | MVP role |
|---|---|---|
| **KW** | Knowledge Worker | Primary chat consumer — asks, summarizes, drafts. |
| **NH** | New Hire | Onboarding via grounded answers over company context. |
| **MGR** / **EXEC** | Manager / Executive | Decision-ready answers across teams. |
| **ADM** | Admin | Configures tenant, connects sources, manages permissions & rollout. |
| **SEC** | Security / Compliance | Consumes the audit trail; owns AI governance. |

Full persona legend (23 tags) lives in [user-stories.md](../product/user-stories.md); the rest are roadmap personas attached to out-of-scope epics.

## 6. Non-goals (what Lumen Copilot is *not*)

- **Not a system of record.** It reads across sources; it does not own the canonical copy of anything.
- **Not an autonomous actor in the MVP.** It answers and drafts; it does not take consequential action without approval.
- **Not a permissions replacement.** It *mirrors and enforces* source ACLs; it never grants access a source wouldn't.
- **Not a cross-tenant learner.** Tenant data never trains models shared across tenants or leaks across the tenancy boundary.

## 7. Sequencing

This scope maps onto the existing M0–M5 ladder ([consolidated-structure.md](../product/consolidated-structure.md) §10), re-weighted for the grounded-chat MVP: M0 front-loads the tenancy/permission/ingestion/upload/audit cross-cuttings; M1 delivers the grounded chat surface with citations. Stack-shaped specifics are re-confirmed when **OD-2** (stack) closes.

## 8. What this does and does not close

- **Closes:** OD-1 (product scope & mission adjectives).
- **Does not close — still ask, don't default:** OD-2 (stack), OD-3 (boundaries), OD-4 (security & domain invariants — derived from *this* scope), OD-5 (local-run/verify), OD-7 (CI). OD-1 closing is the precondition that lets OD-2 and OD-4 proceed.

---

## Provenance

- **Decided by:** human sponsor + Claude Opus 4.8, in session, 2026-06-17.
- **Inputs:** the discovery corpus in `docs/product/` (16 epics / 136 stories, competitive research, consolidated structure).
- **Traceability:** issue [#11](https://github.com/k-sandhu/lumen-copilot/issues/11); strikes OD-1 in [0001-open-decisions.md](0001-open-decisions.md); fills [AGENTS.md](../../AGENTS.md) §2.
