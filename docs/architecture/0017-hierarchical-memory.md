# 17. Hierarchical memory (org / assistant / user) with governed promotion

- **Status:** Accepted *(direction approved by the sponsor in-session 2026-07-16; amended same day for the [#417 review](https://github.com/k-sandhu/lumen-copilot/pull/417) findings before first merge — immutable thereafter; changes supersede. Build gated on [epic #408](https://github.com/k-sandhu/lumen-copilot/issues/408) decomposition)*
- **Date:** 2026-07-16
- **Builds on:** [ADR-0016](0016-context-engine-and-cache-first-prompting.md) (the reserved `memory` segment; cache-aligned boundaries), [ADR-0004](0004-architecture-boundaries-and-adapters.md) (module ownership: `db/` owns relational, `search/` is the only OpenSearch adapter, `retrieval/` is the permission chokepoint), [ADR-0010](0010-dedicated-text-search-engine.md), [ADR-0011](0011-assistant-and-agent-runtime.md), [ADR-0012](0012-mcp-integration.md), [ADR-0015](0015-scheduling-and-headless-runs.md), [spec 0004](../specs/0004-security-and-domain-invariants.md) (INV-1/2/3/6/7)
- **Scope:** SPIKE — the only deliverable of [#406](https://github.com/k-sandhu/lumen-copilot/issues/406); no product code, no migrations. This ADR also **mandates a spec-0004 amendment (the INV-M family, §5) in the first memory PR**. Feature decomposition for [#408](https://github.com/k-sandhu/lumen-copilot/issues/408) is against this ADR.

## Context

Every leading assistant ships memory, and the mechanics converged: **extraction off the hot path** by a cheap model, **explicit agent writes** (Anthropic's memory tool), **memory paired with compaction**, **user-visible/deletable entries**, and **temporal supersession** (Zep). What nobody ships well is **organizational** memory with governed promotion between scopes — this is our differentiating move, and it is chiefly a *governance* problem, which is this codebase's strength.

The hard constraints this ADR must not violate (all flagged in review, all load-bearing): INV-3 (no asserted fact without a resolvable citation to a *permitted passage*), INV-2 (retrieval admits only what the requester may currently see, and an assistant's `collection_ids` *narrow* that further), INV-6 (every retrieval and denial audits), and INV-7 (no consequential action without a *recorded* approval). Memory also introduces a **persistent prompt-injection channel** a naive design would amplify.

## Decision

### 1. Scopes, precedence, and ship order

Four levels, most-specific-wins:

| Scope | Holder | Written by | Visible to | Ships |
|---|---|---|---|---|
| `org` | tenant | admin curation; **approval-gated promotion** (§3) | the whole tenant | third |
| `assistant` | an assistant | approval-gated promotion; assistant owners | sessions of that assistant | second |
| `user` | a user | async extraction (constrained, §6) + `remember` tool + the user | **that user only** | **first (MVP)** |
| session | a chat session | ADR-0016 §3.2 rolling summary | that session | already decided (0016) |

**Precedence is deterministic and schema-backed, not prose:** every entry carries a **`memory_key`** (a normalized subject/conflict-group id). Within a `memory_key`, the effective entry is chosen by: scope specificity (user > assistant > org) → `pinned` → higher `confidence` → newer `created_at` → lexical id tiebreak. A `superseded_by` self-reference marks the retired side of a temporal conflict; a partial-unique index enforces one *current head* per `(tenant_id, scope, scope_ref, memory_key)` where `superseded_by IS NULL`.

### 2. Ownership & the explicit dependency path (ADR-0004)

New owning module `backend/app/memory/` (+ AGENTS.md §6 row **in the first code PR — human approval required, §5**). It **owns memory policy only**; it does not open a second data path:

- **Writes (source of truth):** `memory service → db/ repositories` (a new tenant-scoped `MemoryRepository`; `memory_entries` is an ordinary RLS-backed table).
- **Indexed reads:** `memory service → retrieval/ (permission-checked query) → search/ adapter`. Memory is a scoped corpus in the existing OpenSearch family with `tenant_id` + scope + `scope_ref` filter fields; a read is **OpenSearch candidate fetch → Postgres hydration → current-permission recheck** (the same shape retrieval already uses for documents). There is **no** memory-specific search client. Stale-index and revocation tests required.

Typed holder columns, not a polymorphic ref: `user_scope_ref FK→users` and `assistant_scope_ref FK→assistants`, each with a CHECK that exactly the column matching `scope` is non-null and same-tenant — so the wrong-FK / cross-tenant shape is unrepresentable.

### 3. Write paths & the real promotion mechanism

1. **Async extraction** (Celery, post-answer, tenant-bound scope; **every model call goes through the `llm/` gateway**, is metered via #409, and is audited) — **constrained in MVP (§6)**.
2. **The `remember` tool** — explicit, model-invocable (registry `ToolDefinition`, **T1**, `read_only=False`, `default_offered=False`), autonomy-gated by the existing runner path, surfaced as a visible event.
3. **Promotion is a first-class, recorded approval — not the current `PolicyApprovalGate`.** The live `PolicyApprovalGate` only reads tenant tool policy and allows a pre-enabled action; it records no approver, binds no payload, and cannot expire — so it **cannot** govern promotion (INV-7). This ADR therefore **defers all promotion (assistant/org scope) behind a real approval record** — a `memory_promotion_requests` state machine carrying: actor, immutable **payload hash**, source scope+ref, destination scope, **the provenance proof (§ below)**, decision (pending/approved/denied), decider, and expiry. Promotion executes only on an explicit human `approved` decision bound to the unchanged payload hash; replay/mutation/expiry all deny. Until that mechanism ships, **only `user`-scope memory exists** (the MVP), and promotion is disabled.

**Promotion cannot launder access (INV-2).** Approval authorizes *widening the audience*; it does not grant the destination audience access to the source. Therefore promotion requires **structured provenance** and, at the destination, **read-time re-validation for every reader**:

- An entry derived from a **document** may be promoted only if promotion carries proof the destination audience can read that source; a `user`-authored utterance carries the author as provenance. Document-derived promotion whose destination-access cannot be proven is **refused**.
- At read time, an `assistant`/`org` entry whose provenance is a document is admitted to a given reader only if that reader currently passes the source's permission predicate (revoked source ⇒ stripped). Leakage tests required: user→assistant, user→org, and revoked-source-after-promotion.

### 4. Read path — the ADR-0016 `memory` segment

At assembly, per scope the run may see: pinned + top-k relevant entries (query = the question) under per-scope token budgets, rendered as one labelled block **framed as untrusted data about the user/org, not instructions** (§6). Updates land only between answers (extraction async, promotion out-of-band), so the segment is version-stable within an answer (cache-aligned). Reads honor §3's per-reader revalidation.

### 5. Invariants — the INV-M family (spec 0004 amendment, in the first memory PR)

- **INV-M1 (isolation):** a `user`-scope entry never surfaces to another user; `assistant`/`org` entries never cross tenants (INV-1); document-provenanced entries honor §3 per-reader source-access at read time (INV-2).
- **INV-M2 (audit — reads included):** memory **reads are retrievals** and audit like any retrieval (INV-6): `memory.retrieved` (query hash + returned ids/scopes), plus denied/error outcomes, extraction decisions, supersession, expiry, and purge — **never raw memory content**. `last_used_at` maintenance is a write and is covered.
- **INV-M3 (context, not source — INV-3 preserved):** memory **steers** retrieval and shapes tone; it is **never itself a citation**. Concretely: **non-factual `user`-scope memory** (preferences, role, style) may be uncited because it asserts no external fact; **a factual claim still requires a resolvable citation to a permitted passage** — the model may *use* a memory to decide *what to search*, but the answer's facts cite retrieval. A **negative test blocks a memory-only factual assertion** from being emitted as a grounded claim.
- **INV-M4 (provenance + transparency):** every entry carries structured provenance; users view/edit/delete their own entries (the [#296](https://github.com/k-sandhu/lumen-copilot/issues/296) panel is the surface). GDPR: see §7.

### 6. The injection surface — structural controls, not prompt hygiene

Model-written memory is a persistent injection channel, and "store facts not imperatives" is **not** a control. Structural mitigations (all required, all tested):

- **MVP automatic extraction is limited to the user's own authored utterances.** Document-derived candidates are **quarantined** — not written automatically; they require explicit user confirmation (or an admin curation step) before becoming an entry.
- Candidates are **rejected if instruction-, action-, or secret-shaped** (imperative verbs, tool/URL/credential patterns) by a validator, before storage.
- The memory block is rendered as **delimited untrusted data**; the system prompt states memory is descriptive, never a command.
- A **tenant kill-switch** disables memory read/write instantly.
- Tests: multi-turn poisoning, indirect injection via an uploaded document, secret-retention, and confirm the quarantine holds.

### 7. Purging & lifecycle (the real GDPR contract)

Deletion is a **purge workflow**, not a single row delete: immediate read denial (tombstone) → delete from Postgres → delete the OpenSearch document + embeddings → invalidate any derived caches, rolling summaries, and evidence digests that referenced it → terminal completed/error state with retry. Backups follow the documented retention window (a delete cannot reach into an immutable backup; that window is stated, not silently promised away). TTL by kind, relevance decay via `last_used_at`, per-scope caps (LRU). Purge audits **ids and outcomes only**, never content.

## Consequences

- The differentiator — governed org memory with recorded, access-preserving promotion — lands on existing governance machinery, but **the real approval state machine (§3) is net-new work** and gates any scope above `user`. The MVP is user-scope only.
- Costs: one cheap async extraction call per answered turn (gatewayed, metered, audited), a scoped OpenSearch index, a real injection surface (mitigated + tested), and a purge pipeline touching every derived store.
- Read cost: ≤ ~1.4k tokens of budgeted, cache-stable prefix, paid back by ADR-0016 caching.
- Explicitly out of v1 (each a later decision): personalization/ranking boosts, automatic document-derived extraction without confirmation, any promotion before §3's mechanism exists, memory-as-citation-source (INV-3 stands).
