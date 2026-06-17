# Spec 0003 — Product Scope & Mission (closes OD-1)

> Closes **OD-1** in [0001-open-decisions.md](0001-open-decisions.md). Decided with the human sponsor in session on **2026-06-17**. This spec defines *what Lumen Copilot is*, the *first buildable scope*, and the *decision-filters* that let an agent make novel calls aligned with intent (it fills [AGENTS.md](../../AGENTS.md) §2).

**Status:** adopted. **Last reviewed:** 2026-06-17. **Tracking issue:** [#11](https://github.com/k-sandhu/lumen-copilot/issues/11).

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
- **Autonomous, scheduled & event-driven agents** (E7) and the **agent builder / reusable skills** (E6).
- **Computer use & browser/desktop automation** (E16).
- **Proactive work intelligence** (E4) and **meetings/communication intelligence** (E10).
- **Departmental automation packs** (E12).
- **Deep research & artifact-generation suites** (E8, E9) beyond in-chat summarize/draft.
- **Public developer platform / external API & embedding** (E15).
- **Advanced governance & AgentOps** (E14) beyond the M0 audit log.

The full 16-epic vision in [consolidated-structure.md](../product/consolidated-structure.md) remains the **roadmap**; this spec sets only the *first* demoable scope.

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
