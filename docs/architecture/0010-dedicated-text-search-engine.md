# 10. Dedicated text-search engine behind the retrieval seam

- **Status:** Accepted *(sponsor decisions recorded 2026-07-02; supersedes the retrieval-store choice in [ADR-0003](0003-application-stack.md) and the "Vector + lexical retrieval → `retrieval/`" boundary row in [ADR-0004](0004-architecture-boundaries-and-adapters.md))*
- **Date:** 2026-06-28 *(proposed)* · 2026-07-02 *(accepted)*
- **Builds on:** [ADR-0003](0003-application-stack.md) (the stack reserved this seam), [ADR-0004](0004-architecture-boundaries-and-adapters.md) (module boundaries), [ADR-0005](0005-local-run-and-developer-workflow.md) (one-command local stack), [ADR-0008](0008-conflict-free-parallel-delivery.md) (serialized seam → parallel build), [spec 0004](../specs/0004-security-and-domain-invariants.md) (INV-1 tenant isolation, INV-2 owner-or-grant, INV-3 citations)

## Context

Search today is **hybrid retrieval inside one module**: `backend/app/retrieval/` runs a `pgvector` cosine-ANN leg (semantic) and a Postgres full-text leg (`to_tsvector('english', …)` ⊕ `plainto_tsquery`), fuses the two rankings with Reciprocal Rank Fusion (`retrieval/fusion.py`), and applies the INV-1/INV-2 permission predicate in the **same** `WHERE` clause (`retrieval/queries.py` — `tenant_id` AND `owner_id ∈ allow-set OR an explicit/collection grant`). It is the **single chokepoint**: there is no unfiltered query path, which is what makes the permission guarantee provable, and citations inherit it because they are drawn only from retrieved passages.

[ADR-0003](0003-application-stack.md) deliberately chose Postgres + pgvector for the MVP **and reserved this seam**:

> "Hybrid … pgvector semantic + Postgres full-text … behind a retrieval adapter so a dedicated engine (Qdrant/OpenSearch) can replace it when corpus scale demands, **without touching callers**."

Postgres full-text has known ceilings as the corpus grows: `plainto_tsquery` gives no phrase/proximity, fuzzy/typo-tolerance, or synonym expansion; relevance tuning is limited (no BM25F field boosting); there is no native highlighting, faceting, or did-you-mean; and lexical recall/latency degrade at scale. The sponsor has decided to adopt a dedicated engine now.

## Decision

Adopt **OpenSearch as the single retrieval store** for both the lexical (BM25) and semantic (kNN vector) legs, behind the existing retrieval seam. `pgvector` is retired as the vector store. The decisions below are final (see *Resolved decisions*).

### 1. Engine — **OpenSearch** (Apache-2.0)

| Engine | License | Hybrid (BM25 + kNN) | Footprint | Notes |
|---|---|---|---|---|
| **OpenSearch** *(chosen)* | Apache-2.0 (OSI) | Yes (BM25 + `knn_vector` + hybrid normalization/RRF search pipeline) | Heavy (JVM, ~1–2 GB) | ADR-0003-named candidate; mature relevance tuning, highlighting, analyzers |
| Meilisearch | MIT (OSI) | Limited (vectors experimental) | Light | Weaker large-scale relevance control |
| Typesense | GPL-3.0 (OSI) | Yes (basic) | Light | Smaller ecosystem |
| Elasticsearch | ELv2 / SSPL (not OSI) | Yes | Heavy | **Rejected — licensing conflicts with ADR-0003's OSS-only constraint** |

OpenSearch is the only OSI-licensed option with first-class **hybrid** search (BM25 + native kNN in one store) plus the relevance controls that justify leaving Postgres FTS — which is what makes the *single-store* decision viable.

### 2. Single retrieval store

OpenSearch holds, per chunk, both the **analyzed text** (BM25) and the **embedding** (`knn_vector`). Hybrid ranking is done by OpenSearch's **hybrid query + normalization search pipeline** (score-normalized BM25 ⊕ kNN), replacing the Python `pgvector`+FTS+RRF path. Embeddings are still produced by the `llm/` gateway (bge-m3) and written into OpenSearch at index time. The `pgvector` extension, the `chunks.embedding` column, and the Postgres FTS query path are **removed** once cutover is verified.

### 3. Module boundary — new `backend/app/search/`; `retrieval/` stays the chokepoint

- `backend/app/search/` — owns the OpenSearch client: index create/mapping, upsert/delete, and the hybrid query. Exposes **domain types only**; never leaks OpenSearch response objects upward.
- `retrieval/` continues to own the **permission predicate, query orchestration, and passage hydration**. It builds the allow-set filter and hands it to `search/`; it remains the **single place** any retrieval query is issued from — no caller can query OpenSearch directly. Fusion moves into the OpenSearch hybrid pipeline; `retrieval/` still normalizes results into `RetrievedPassage`.
- **Boundary-table change (supersedes ADR-0004 §6):** the retrieval **store** is now `backend/app/search/` (OpenSearch); `retrieval/` remains the permission chokepoint and orchestrator. This ADR is the record of that shift (ADR-0004 is immutable; it is superseded here).

### 4. Permission enforcement at the engine (INV-1/INV-2) — **load-bearing**

The engine MUST NOT return a passage the caller could not retrieve. **Query-time filtering, deny-by-default**, mirroring the SQL predicate exactly:

- **every** query carries a mandatory `tenant_id` term filter (INV-1) — there is no engine query without it (a query builder that can omit it is a defect);
- plus an allow filter: `owner_id == caller` **OR** `document_id ∈ granted-docs` **OR** `collection_id ∈ granted-collections`, where the grant id-sets are resolved from the `grants` table per request (the SQL `EXISTS` becomes a resolved id-set `terms` filter);
- a revoked grant drops the id from the set → excluded again (deny-by-default preserved);
- the filter is built in **one** `search/` query helper the retrieval chokepoint calls.

Retrieved chunks are still hydrated and permission-re-checked against Postgres (the source of truth for ownership/grants) before becoming citations — defense in depth. **Parity negative tests are mandatory** (the INV-1/INV-2/INV-3 categories, spec 0004 §9): cross-tenant excluded, non-owned/non-granted excluded, revoked-grant excluded, citation offsets exact. A bypass is a blocking defect — same bar as the SSRF chokepoint in [ADR-0009](0009-connector-framework-and-web-source.md).

### 5. Index model, indexing, reindex

- **Document = chunk** (the citation unit): `{ chunk_id, tenant_id, document_id, owner_id, collection_id, ord, text (analyzed), embedding (knn_vector), char_start, char_end }`. Offsets are stored so highlights/snippets map back to exact source spans (INV-3).
- **Topology (decided):** a **single shared index** with a **mandatory `tenant_id` filter** on every query (routing by tenant). Per-tenant indices are a future option only if isolation/scale demands.
- **Write path:** a **Celery task** ([tasks/](../../backend/app/tasks/)) upserts/deletes chunk docs (text + embedding + metadata) on ingest and document mutation — never in the request path. Document/source deletion cascades to index deletes.
- **Backfill:** an idempotent, resumable reindex command for the existing corpus.

### 6. Local stack + config (base stack)

- New **`opensearch`** service in `docker-compose.yml` as part of the **default stack** (pinned image, single-node `discovery.type=single-node`, security demo config disabled for local, healthcheck, named volume; `backend`/`worker` gain a `depends_on: opensearch (healthy)`). Consistent with [ADR-0005](0005-local-run-and-developer-workflow.md) and the no-`:latest` rule. Sized modestly for a laptop (bounded JVM heap).
- Config via `core/config.py` (`pydantic-settings`): endpoint, credentials, index name, timeouts, kNN params. Because retrieval is now single-store, an **unreachable OpenSearch fails retrieval closed** (surfaced as a retrieval error, never an unfiltered fallback) — see Consequences.

### 7. Cutover (sequenced, ADR-0008)

Serialized seam → parallel build, main green at each step:
1. **Seam:** compose `opensearch` + config + `app/search/` adapter + index mapping (no behavior change yet).
2. **Index:** ingestion/task write-path + backfill/reindex command (dual-write; Postgres still authoritative).
3. **Swap:** `retrieval/` issues the OpenSearch hybrid query with the permission filter; live INV-1/INV-2/INV-3 re-proofs against OpenSearch.
4. **Agentic tools:** `search_text` (hybrid) moves to the new store; `search_documents`/`get_document` keep their relational queries but are verified to share the same tenant+grant filter path (consistency).
5. **Retire pgvector:** drop the `embedding` column + pgvector extension + FTS path once the swap is verified.

## Consequences

- **Upside:** one store for hybrid retrieval — better lexical relevance (BM25F, phrase/proximity, typo-tolerance), native highlighting, native score-normalized hybrid ranking (no bespoke Python fusion), and headroom at scale. Callers are unchanged (the seam was reserved for exactly this).
- **Cost / risk:**
  - **OpenSearch becomes a hard dependency of all retrieval** (chat grounding + `/search` + agent tools). Single-store means **no Postgres fallback** — if OpenSearch is down, retrieval fails closed. This is the deliberate trade for the sponsor's "single retrieval store" decision; availability/health monitoring matters more now.
  - A **~1–2 GB JVM service in the default stack** raises the local-run floor ([ADR-0005](0005-local-run-and-developer-workflow.md)); mitigated with a bounded heap and single-node config.
  - **Index consistency**: Postgres remains the source of truth for documents/permissions; the index is derived and must be reconciled (dual-write + reindex).
  - The **permission guarantee must be re-proven at the engine** with negative tests — the main correctness risk.
- **Delivery:** a multi-slice epic per [ADR-0008](0008-conflict-free-parallel-delivery.md) (§7 sequence above), each slice its own issue/PR with `Closes #`.

## Resolved decisions (sponsor, 2026-07-02)

1. **Go / no-go:** **Go** — adopt a dedicated engine now.
2. **Engine:** **OpenSearch.**
3. **Local-stack footprint:** ship it in the **base stack** (default `docker compose up`), not an opt-in profile.
4. **Index topology:** **single shared index with a mandatory `tenant_id` filter** (per-tenant indices deferred).
5. **Vectors:** **single retrieval store** — fold embeddings into OpenSearch kNN; retire `pgvector` as the vector store.
6. **Agentic tools:** the retrieval agent tools are updated onto the same store and permission path in the same epic.
