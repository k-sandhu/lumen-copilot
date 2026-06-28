# 10. Dedicated text-search engine behind the retrieval seam

- **Status:** Proposed *(awaiting sponsor decision — go/no-go + engine choice; no code until accepted)*
- **Date:** 2026-06-28
- **Builds on:** [ADR-0003](0003-application-stack.md) (the stack reserved this seam), [ADR-0004](0004-architecture-boundaries-and-adapters.md) (the `retrieval/` boundary owns vector + lexical retrieval), [ADR-0005](0005-local-run-and-developer-workflow.md) (one-command local stack), [ADR-0008](0008-conflict-free-parallel-delivery.md), [spec 0004](../specs/0004-security-and-domain-invariants.md) (INV-1 tenant isolation, INV-2 owner-or-grant, INV-3 citations)

## Context

Search today is **hybrid retrieval inside one module**: `backend/app/retrieval/` runs a `pgvector` cosine-ANN leg (semantic) and a Postgres full-text leg (`to_tsvector('english', …)` ⊕ `plainto_tsquery`), fuses the two rankings with Reciprocal Rank Fusion (`retrieval/fusion.py`), and applies the INV-1/INV-2 permission predicate in the **same** `WHERE` clause (`retrieval/queries.py` — `tenant_id` AND `owner_id ∈ allow-set OR an explicit/collection grant`). It is the **single chokepoint**: there is no unfiltered query path, which is what makes the permission guarantee provable, and citations inherit it because they are drawn only from retrieved passages.

[ADR-0003](0003-application-stack.md) deliberately chose Postgres + pgvector for the MVP **and reserved this seam**:

> "Hybrid … pgvector semantic + Postgres full-text … behind a retrieval adapter so a dedicated engine (Qdrant/OpenSearch) can replace it when corpus scale demands, **without touching callers**."

Postgres full-text is the right MVP choice but has known ceilings as the lexical surface grows: `plainto_tsquery` gives no phrase/proximity, fuzzy/typo-tolerance, or synonym expansion; relevance tuning is limited (no BM25F field boosting); there is no native highlighting, faceting, or did-you-mean; and lexical recall/latency degrade at large corpus size. A **dedicated text-search engine** addresses these — at the cost of a second system to run, index, and secure.

This ADR proposes adopting one **behind the existing seam** and, because it is a costly, hard-to-reverse choice, surfaces the decisions only the sponsor can make. **It is a proposal: no code lands until it is Accepted.**

## Decision (proposed)

### 1. Scope the first step to the **lexical** leg only

Replace the **Postgres full-text leg** with a dedicated engine; keep the **`pgvector` semantic leg** and **RRF fusion** unchanged. This is the smallest reversible step, keeps embeddings where they already live, and isolates the blast radius. Moving vectors into the engine's kNN (collapsing both legs into one system) is a **later** decision, not this one.

### 2. Engine — recommend **OpenSearch**

| Engine | License | Hybrid (BM25 + kNN) | Footprint | Notes |
|---|---|---|---|---|
| **OpenSearch** *(recommended)* | Apache-2.0 (OSI) | Yes (BM25 + kNN + RRF/normalization pipeline) | Heavy (JVM, ~1–2 GB) | The ADR-0003-named candidate; mature relevance tuning, highlighting, analyzers |
| Meilisearch | MIT (OSI) | Limited (vectors experimental) | Light, fast, great DX | Excellent typo-tolerance/instant-search; weaker large-scale relevance control |
| Typesense | GPL-3.0 (OSI) | Yes (basic) | Light | Typo-tolerant; smaller ecosystem |
| Elasticsearch | ELv2 / SSPL (not OSI-OSS) | Yes | Heavy | **Rejected: licensing conflicts with ADR-0003's OSS-only constraint** |

**OpenSearch** is recommended: it is the only OSI-licensed option with first-class hybrid search *and* the relevance controls (analyzers, BM25F, highlighting) that justify leaving Postgres FTS, and it is the candidate ADR-0003 already named. **Meilisearch** is the credible lighter alternative if the sponsor prioritizes local-stack simplicity over relevance depth — see Open Questions.

### 3. Module boundary — new `backend/app/search/`, retrieval stays the chokepoint

Per [ADR-0004](0004-architecture-boundaries-and-adapters.md), a new external system gets **one owning module and a new boundary-table row in the same change**. Add:

- `backend/app/search/` — owns the engine client (index, query, delete, health). Exposes **domain types only**; never leaks the engine's response objects upward.
- `retrieval/` continues to own **fusion and the permission predicate**. It calls `search/` for the lexical leg and `pgvector` for the semantic leg, fuses, and returns `RetrievedPassage`s. **The permission chokepoint does not move** — `retrieval/` remains the single place that builds the allow-set filter, so the invariant stays provable.

New boundary-table row (ADR-0004 §6): *Dedicated text-search index → `backend/app/search/`*. The existing "Vector + lexical retrieval → `retrieval/`" row stays (vectors + fusion remain there).

### 4. Permission enforcement at the engine (INV-1/INV-2) — **load-bearing**

The engine MUST NOT be able to return a passage the caller could not retrieve. **Query-time filtering, deny-by-default**, mirroring the SQL predicate exactly:

- **every** query carries a mandatory `tenant_id` term filter (INV-1) — there is no engine query without it;
- plus an allow filter: `owner_id == caller` **OR** `document_id ∈ granted-docs` **OR** `collection_id ∈ granted-collections`, where the grant id-sets are resolved from the `grants` table per request (the SQL `EXISTS` becomes a resolved id-set filter);
- a revoked grant drops the id from the set → excluded again (deny-by-default preserved);
- this filter is built in **one** `search/` query helper that the retrieval chokepoint calls — no caller can issue an unfiltered engine query, exactly as today.

Indexed documents are still hydrated/permission-re-checked against Postgres before becoming citations (defense in depth). **Parity negative tests are mandatory** (the INV-1/INV-2/INV-3 categories from spec 0004 §9) and a bypass is a blocking defect — same bar as the SSRF chokepoint in [ADR-0009](0009-connector-framework-and-web-source.md).

### 5. Index model, indexing, reindex

- **Document = chunk** (the citation unit): `{ chunk_id, tenant_id, document_id, owner_id, collection_id, ord, text, char_start, char_end }`. Offsets are indexed so highlights/snippets map back to exact source spans (INV-3).
- **Topology:** start with a **single shared index with a mandatory `tenant_id` filter** (+ routing by tenant) for operational simplicity; escalate to **per-tenant indices** only if isolation or scale demands it (Open Question).
- **Write path:** a **Celery task** ([tasks/](../../backend/app/tasks/)) indexes/updates/deletes on ingest and document mutation, alongside the existing parse→chunk→embed→pgvector pipeline — never in the request path.
- **Backfill:** a one-shot reindex command for the existing corpus, idempotent and resumable.

### 6. Local stack + config

- New **`opensearch`** service in `docker-compose.yml` (pinned image, single-node, `discovery.type=single-node`, security demo config disabled for local, healthcheck, named volume) — consistent with [ADR-0005](0005-local-run-and-developer-workflow.md)'s one-command stack and the no-`:latest` rule.
- Config via `core/config.py` (`pydantic-settings`): endpoint, credentials, index name, timeouts. Absent/unreachable engine ⇒ the lexical leg fails **closed** (empty lexical results; semantic leg still serves), never an unfiltered fallback.

### 7. Reversible cutover

- A config flag `RETRIEVAL_LEXICAL_BACKEND = postgres | opensearch` (default `postgres`).
- **Shadow phase:** dual-index and dual-query, compare result sets/latency offline, keep Postgres authoritative.
- **Flip** the flag per environment once parity (incl. the permission negatives) holds. Rollback = flip back. The `pgvector` semantic leg and all callers are untouched throughout.

### 8. Deferred (separate decisions, not this scope)

Moving **vectors** into the engine's kNN (single-store retrieval); synonyms/analyzers tuning; faceting; did-you-mean; cross-tenant analytics. Each is additive behind the same seam.

## Consequences

- **Upside:** materially better lexical relevance (BM25F, phrase/proximity, typo-tolerance), native highlighting, and headroom at corpus scale — the ceilings Postgres FTS hits. Callers are unchanged (the seam was reserved for exactly this). The cutover is flag-reversible.
- **Cost / risk:** a **second heavyweight service** (OpenSearch is JVM, ~1–2 GB RAM) weighs on the "one `docker compose up`, runs on a laptop" promise ([ADR-0005](0005-local-run-and-developer-workflow.md)) — Meilisearch is far lighter if that matters more than relevance depth. A second source of truth for text introduces **index-consistency/reindex** burden. The **permission guarantee must be re-proven at the engine** with negative tests (the main correctness risk). Operational surface grows (health, snapshots, version pinning).
- **Why an ADR:** this is a costly, not-self-evident technology + boundary choice (ADR README criteria) and an explicit scope expansion of ADR-0003's deferred seam — it must be decided before code, not discovered in a PR.

## Open questions for the sponsor (decide before this is Accepted)

1. **Go / no-go now:** ADR-0003 said "when corpus scale demands." Is the current corpus/relevance actually demanding this, or does Postgres FTS still suffice for the near term?
2. **Engine:** **OpenSearch** (recommended — most capable, OSI, ADR-named) vs **Meilisearch** (lighter, simpler local footprint, weaker relevance depth)?
3. **Local-stack footprint:** acceptable to add a ~1–2 GB JVM service to the default `docker compose up`, or should it be an **opt-in profile** so the base stack stays light?
4. **Index topology:** shared index + tenant filter (simpler) vs per-tenant indices (stronger isolation, more ops) — start shared, or require per-tenant from day one?
5. **Vectors:** keep `pgvector` for the semantic leg (proposed), or fold vectors into the engine's kNN in the same effort?
