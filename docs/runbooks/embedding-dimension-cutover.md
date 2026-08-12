# Embedding-dimension cutover (1,024 → native 2,048)

Issue #346 fixes the storage/provider drift that left ingestion in
`processing`. The canonical production contract is:

| Boundary | Required value |
|---|---|
| Route | `openai/nvidia/nemotron-3-embed-1b:free` |
| Provider output | exactly 2,048 floats (`encoding_format=float`) |
| Runtime + ORM | 2,048 |
| PostgreSQL active column | `chunks.embedding vector(2048)` |
| PostgreSQL rollback column | `chunks.embedding_legacy_1024 vector(1024)` |
| OpenSearch target | `lumen-chunks-v2`, `knn_vector.dimension=2048` |

There is no padding, truncation, or cross-space cast. Migration 0044 renames
the populated 1,024 column intact, adds a nullable native column, and drops only
the obsolete 1,024 pgvector HNSW index. OpenSearch remains the retrieval store.

## Preflight and lock window

1. Back up PostgreSQL and confirm the backup can be restored. Record counts:

   ```sql
   SELECT count(*) AS chunks,
          count(embedding) AS legacy_vectors
     FROM chunks;
   ```

2. Set `LLM_EMBEDDING_MODEL`, `LLM_EMBEDDING_DIMENSIONS=2048`, and
   `OPENSEARCH_INDEX=lumen-chunks-v2` consistently for API and workers.
3. Pause ingestion workers before `alembic upgrade head`. The column rename/add
   is metadata-only, but PostgreSQL takes an `ACCESS EXCLUSIVE` table lock; the
   old HNSW index drop also waits on concurrent users. Schedule a bounded quiet
   window and inspect blockers before proceeding.
4. Apply the migration, then call `/health/ready`. Readiness must report the
   schema, OpenSearch mapping, and configured provider at 2,048. A mismatch is a
   stop condition, not a reason to coerce vectors.

   ```powershell
   cd backend
   uv run alembic upgrade head
   Invoke-RestMethod http://localhost:47180/health/ready
   ```

## Controlled re-embedding and index cutover

The operator command is read-only by default. It reports only counts/opaque
ids internally; it never logs document text, vectors, or credentials.

```powershell
cd backend
uv run python -m app.ingestion.reembed
uv run python -m app.ingestion.reembed --execute --limit 200
```

Execution first probes PostgreSQL, OpenSearch, and the provider contract, then
publishes the existing idempotent ingestion job for one bounded page. For a
legacy document, ingestion updates deterministic chunks in place: chunk ids and
the 1,024 vector remain unchanged while the 2,048 vector is filled. A changed
chunk shape fails terminally with `legacy_chunk_shape_mismatch` instead of
deleting rollback data.

Wait for workers to drain, rerun the preview, and repeat bounded pages until it
reports zero. If interrupted, rerun it: selection requires a legacy vector and a
missing native vector, while the ingestion task replaces/updates idempotently.
Monitor `ingestion.attempt_failed`, `embedding.*_dimension_mismatch`, and
`ingestion.stranded_documents_redriven`; the latter includes pending/processing
counts beyond `CONNECTOR_INGEST_RECOVERY_MINUTES` without document content.

Finally converge the versioned OpenSearch index and run the tenant-isolated
retrieval/citation smoke:

```powershell
uv run python -m app.search.reindex
```

Do not delete the old `lumen-chunks` index or the legacy PostgreSQL column during
this rollback window. Record old/new vector counts and retrieval evidence before
resuming normal ingestion traffic.

## Rollback and rehearsal

Pause workers and stop API traffic before rollback. Migration 0044 downgrade:

- renames active `embedding vector(2048)` to `embedding_2048` (preserved);
- restores `embedding_legacy_1024` to `embedding` without changing values;
- recreates the old 1,024 HNSW index;
- removes only the new ingestion diagnostic columns.

```powershell
cd backend
uv run alembic downgrade 0043_code_run_resolved_packages
```

Deploy the prior application/config and point it at the retained old OpenSearch
index. A subsequent `alembic upgrade head` restores both parked vector sets,
which is covered by the populated disposable-PostgreSQL migration test. Never
manually cast, slice, pad, truncate, or drop either vector column to recover.

## Verification ledger template

Record exact commands and outputs for:

- offline migration DDL (no `array_fill`/`subvector`);
- disposable PostgreSQL empty and populated upgrade/downgrade/re-upgrade;
- adapter 1,024/2,049 negative cases and readiness drift negatives;
- six-format plus public-source ingestion and terminal failure matrix;
- INV-1/INV-2 retrieval tests;
- isolated upload → ready → retrieval → citation → teardown smoke;
- Ruff, changed-file format, mypy, and `docker compose config`.

If a live gate cannot run, record the date, exact blocker, and residual risk; do
not use the retained persona stack or a shared-data `docker compose down -v`.
