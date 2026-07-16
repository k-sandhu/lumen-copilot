# 17. Hierarchical memory (org / assistant / user) with governed promotion

- **Status:** Accepted *(direction approved by the sponsor in-session 2026-07-16; this ADR records it — build is gated on [epic #408](https://github.com/k-sandhu/lumen-copilot/issues/408) decomposition)*
- **Date:** 2026-07-16
- **Builds on:** [ADR-0016](0016-context-engine-and-cache-first-prompting.md) (the assembler's reserved `memory` segment; cache-aligned update boundaries), [ADR-0004](0004-architecture-boundaries-and-adapters.md) (one owning module per concern), [ADR-0010](0010-dedicated-text-search-engine.md) (OpenSearch owns indexed text), [ADR-0011](0011-assistant-and-agent-runtime.md) (assistants; autonomy levels), [ADR-0015](0015-scheduling-and-headless-runs.md) (Celery task infra), [spec 0004](../specs/0004-security-and-domain-invariants.md) (INV-1/2/3/6/7)
- **Scope:** SPIKE — the only deliverable of [#406](https://github.com/k-sandhu/lumen-copilot/issues/406); no product code, no migrations. Feature decomposition for [#408](https://github.com/k-sandhu/lumen-copilot/issues/408) happens against this ADR.

## Context

Every leading assistant now ships memory, and the mechanics have converged: **extraction runs off the hot path** by a cheap model (ChatGPT saved memories; Mem0's ADD/UPDATE/DELETE/NOOP dedup pipeline), **the agent can also write explicitly** (Anthropic's memory tool — the model issues read/write ops, the platform owns storage), memory pairs with compaction so durable facts survive summarization (Anthropic measured 84% token savings + 39% task improvement on 100-turn agents with memory + context editing), entries are **user-visible and deletable** (trust + GDPR), and temporal conflicts are resolved by supersession rather than duplication (Zep). What *nobody* ships well is **organizational memory**: ChatGPT/Claude memory is per-user, Letta/Mem0 namespaces are per-agent, and Glean's org "memory" is implicit in its index. A governed org → assistant → user hierarchy with promotion between levels is the differentiating move — and it is chiefly a **governance** problem, which is this codebase's strength (risk tiers, approval gates, audit, tenant isolation).

Lumen already has proto-memory fragments: per-user custom instructions (preferences), assistant instructions (ADR-0011), and — once ADR-0016 lands — a per-session rolling summary. What is missing is durable, *governed*, cross-session memory.

## Decision

### 1. Scopes, precedence, and ship order

Four levels, most-specific-wins on conflict:

| Scope | Holder | Written by | Visible to | Ships |
|---|---|---|---|---|
| `org` | tenant | admin curation; **approval-gated promotion** from lower scopes | the whole tenant | third |
| `assistant` | an assistant | promotion from user scope; assistant owners | sessions of that assistant | second |
| `user` | a user | async extraction + the `remember` tool + the user | **that user only** | **first (MVP)** |
| session | a chat session | ADR-0016 §3.2 rolling summary | that session | already decided (0016) |

The session summary is the *working* memory tier and is **not** rebuilt here.

### 2. Ownership — a new `backend/app/memory/` module

Memory is a new concern ⇒ a new owning module and a new AGENTS.md §6 row **in the same change** as the first code (per §7.6 — the row edit **requires explicit human approval**, AGENTS.md §5):

| Concern | Single owning module |
|---|---|
| Durable agent/user/org memory (entries, extraction, promotion) | `backend/app/memory/` |

Storage rides **existing infra only**: a `memory_entries` Postgres table (source of truth) + embeddings in the existing OpenSearch index family with ACL fields (scope, scope_ref, tenant) so retrieval reuses the hybrid + permission-filter machinery. No new external system.

Sketch (guidance, not a migration):

```
memory_entries: id, tenant_id, scope (org|assistant|user), scope_ref (assistant_id|user_id|NULL),
                kind (fact|preference|instruction), content (bounded ~1KB), provenance
                (session_id/message_id/document_id), confidence, pinned, superseded_by,
                last_used_at, expires_at, created_at/updated_at, created_by
```

### 3. Write paths (three, all audited)

1. **Async extraction** (Celery, post-answer — never the hot path): a cheap model extracts candidate `user`-scope entries from the finished turn; candidates are deduped against existing entries by similarity and resolved as ADD / UPDATE (supersede) / DELETE / NOOP. Extraction stores **facts, not imperatives** (see §6).
2. **The `remember` tool**: an explicit, model-invocable write (registry `ToolDefinition`, **T1**, `read_only=False`, `default_offered=False` initially) — autonomy-gated by the existing runner path and surfaced as a visible stream event, so "remember that I prefer X" works in-session and is never silent.
3. **Curation & promotion**: users CRUD their own entries; assistant owners curate assistant scope; **promotion** (user → assistant, assistant/user → org) is a consequential action routed through the existing `ApprovalGate` seam (admin approval; INV-7) and audited.

### 4. Read path — the ADR-0016 `memory` segment

At assembly time the runtime pulls, per scope the run can see: pinned entries + top-k semantically relevant entries (query = the current question) under **per-scope token budgets** (order of magnitude: org ~400 / assistant ~400 / user ~600 tokens), rendered as one labelled block with provenance markers. Updates land **only between answers** (extraction is async; promotion is out-of-band), so the segment is version-stable within an answer and cache-aligned by construction. `last_used_at` updates on retrieval (feeds decay).

### 5. Invariants — the INV-M family (spec 0004 amendment, landing with the first memory PR)

- **INV-M1 (isolation):** a `user`-scope entry never surfaces to any other user; `assistant`/`org` entries never cross tenants (INV-1) — negative tests required.
- **INV-M2 (audit):** every write/update/delete/promotion emits a `memory.*` audit event (INV-6 extension).
- **INV-M3 (context, not source):** memory **steers** the answer but is never citable — INV-3 is unchanged: citations come only from retrieval. An answer grounded only in memory is an *uncited* answer and says so.
- **INV-M4 (provenance + transparency):** every entry carries provenance; users can view/edit/delete their entries (the [#296](https://github.com/k-sandhu/lumen-copilot/issues/296) transparency panel is the natural surface — sequence together). This is also the GDPR-portability answer.

### 6. The injection surface, named

Model-written memory is a **persistent prompt-injection channel**: a poisoned document could plant an instruction-shaped "memory" that steers every future run. Mitigations are layered: extraction is instructed (and eval-tested) to store declarative facts, not imperatives; the memory block is framed as *data about the user/org, not instructions*; provenance is mandatory and shown; org scope requires human curation; entries are size- and count-capped. The negative eval ("a document containing 'always exfiltrate X' never becomes an effective memory") joins the harness.

### 7. Purging & lifecycle

TTL by kind (`expires_at`), relevance decay (eviction job over `last_used_at`), per-scope entry caps (LRU beyond cap), supersession instead of contradiction pile-up, tenant-level retention policy setting. Deletion is hard (the row goes; the audit event remains).

## Consequences

- The **differentiator none of the incumbents ship** — org-level memory with approval-gated promotion — lands on governance machinery that already exists (risk tiers, `ApprovalGate`, audit sink, tenant scoping), not new invention.
- Personalization/ranking boosts, memory analytics, and any write-scope widening are **explicitly out** of v1 and would be new decisions behind evals.
- Costs: one cheap-model extraction call per answered turn (async), a small OpenSearch index, and a real (managed) injection surface — accepted with §6's mitigations and tests.
- The read path adds ≤ ~1.4k tokens of budgeted, cache-stable prefix — paid for many times over by ADR-0016's caching.
