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
| Vector-space identity | SHA-256 of provider base + model + dimension + normalization revision |
| OpenSearch target | `lumen-chunks-v2`, `knn_vector.dimension=2048` |

There is no padding, truncation, or cross-space cast. Migration 0044 renames
the populated 1,024 column intact, adds a nullable native column, and drops only
the obsolete 1,024 pgvector HNSW index. OpenSearch remains the retrieval store.
Width alone is never treated as compatibility: every Postgres chunk and
OpenSearch document carries the vector-space fingerprint, and the index mapping
stores the same fingerprint in `_meta.lumen_embedding_space`. Query retrieval
filters to that fingerprint and then hydrates only the exact current, `ready`
Postgres ingestion attempt.

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
4. Apply the migration and start one API process. Startup performs the
   side-effecting compatibility preflight exactly once for the configured
   fingerprint: inspect PostgreSQL, create/validate the versioned OpenSearch
   index and pipeline, then make one provider embedding call. Only complete
   success is cached. A missing key, outage, width mismatch, or same-width
   model/fingerprint mismatch is a stop condition, not a reason to coerce
   vectors.
5. Call `/health/ready`. This endpoint is observational: it reads the schema,
   mapping, ordinary dependencies, and startup's validated fingerprint. It does
   **not** create an index/pipeline or call the provider, so health polling cannot
   incur model spend or poison an old index. After a failed API preflight, a web
   source create or any source resync returns a typed 503 before changing durable
   state. Work accepted before the failure is retried with the bounded ingestion
   backoff policy and terminalized as source `error` on exhaustion; it cannot stay
   `pending` forever. Each worker independently enforces the same preflight before
   claiming ingestion work.

   ```powershell
   cd backend
   uv run alembic upgrade head
   Invoke-RestMethod http://localhost:47181/health/ready
   ```

## Controlled re-embedding and index cutover

The operator command is read-only by default. Its preview reports
`total_requiring` across **all** matching Ready documents separately from the
bounded `page_selected` and `limit`; the total is computed in the same database
snapshot without materializing the full backlog. It reports only counts/opaque
ids internally and never logs document text, vectors, or credentials.

```powershell
cd backend
uv run python -m app.ingestion.reembed
uv run python -m app.ingestion.reembed --execute --limit 200
```

Execution first probes PostgreSQL, OpenSearch, and the provider contract, then
transactionally reserves one `FOR UPDATE SKIP LOCKED` page before broker I/O.
It prints and structurally logs one opaque per-document outcome. An accepted
publish keeps its `pending` reservation; a definite broker failure is released
to `ready`, makes the command exit non-zero, and remains visible on the next
preview. Parallel operators therefore divide the page, and an interrupted run
is resumable without falsely reporting the whole batch published.

For a legacy document with unchanged content, ingestion updates deterministic
chunks in place: chunk ids and the 1,024 vector remain unchanged while the 2,048
vector and its fingerprint are filled. A legitimate connector content revision
archives the old text/spans/vector bytes in
`embedding_legacy_archive_0044`, keyed by content revision + replacement attempt
+ target fingerprint, before replacing chunks. A stale worker fails its
attempt-token compare-and-set before either archive or replacement. This keeps
content revisions working without giving late workers a way to clobber them.

Wait for workers to drain, rerun the preview, and repeat bounded pages until it
reports zero. If interrupted, rerun it: selection includes either a preserved
legacy vector missing its native replacement **or any chunk whose fingerprint
does not equal the configured target**. Thus a same-dimension model/provider or
normalization change fails closed and remains an explicit backfill candidate;
changing only `LLM_EMBEDDING_MODEL` can never silently reuse old vectors.
Monitor `ingestion.attempt_failed`, `embedding.*_dimension_mismatch`, and
`ingestion.stranded_documents_redriven`; the latter includes pending/processing
counts beyond `CONNECTOR_INGEST_RECOVERY_MINUTES` without document content.

Each worker claim increments `documents.ingestion_attempts`; chunk persistence,
OpenSearch ids/publication, Ready/Failed finalization, retries, and stale recovery
all compare that generation. OpenSearch ids are `chunk_id:attempt`. `ready` is
published only after every current-generation bulk succeeds and OpenSearch
acknowledges one index-wide refresh. Therefore the first search after observing
Ready must see that generation; do not use retrieval polling as cutover evidence.
Failed or superseded generations are deleted exactly by attempt where possible and are
always invalidated by the Postgres Ready/attempt/fingerprint hydration gate, so
a late worker cannot publish, delete, or fail a newer result.

The stale-work sweep also reserves its bounded age-qualified page atomically by
renewing `updated_at` in the same `UPDATE ... RETURNING` that selects rows (with
`SKIP LOCKED` on PostgreSQL). Parallel poll beats therefore divide recovery work
instead of enqueueing duplicate deliveries from the same age-only snapshot.
Generic reindex cleanup is attempt-exact for every surviving document; only a
source row proven absent permits a document-wide derived-store delete. A repair
snapshot can consequently never delete a replacement generation published while
the repair is in flight.

Finally converge the versioned OpenSearch index and run the tenant-isolated
retrieval/citation smoke:

```powershell
uv run python -m app.search.reindex
```

Do not delete the old `lumen-chunks` index or the legacy PostgreSQL column during
this rollback window. Record old/new vector counts and retrieval evidence before
resuming normal ingestion traffic.

For a future same-width model rollout, choose a **new versioned OpenSearch index**
and deploy the new fingerprint with ingestion initially gated. Preserve the old
index and a database backup for the rollback window, run the operator until its
preview is zero, prove retrieval/citations in the new index, then admit traffic.
Do not rewrite an existing index's `_meta` fingerprint: that would relabel old
coordinates without re-embedding them. Migration 0044 preserves the 1,024→2,048
rollback set; it is not a general parallel store for two different 2,048 spaces,
so rolling back a later same-width model change requires the recorded backup or
a fresh backfill with the prior model.

## Rollback and rehearsal

Pause workers and stop API traffic before rollback. Migration 0044 downgrade:

- renames active `embedding vector(2048)` to `embedding_2048` (preserved);
- restores `embedding_legacy_1024` to `embedding` without changing values;
- recreates the old 1,024 HNSW index;
- removes only the new ingestion diagnostic columns; and
- **halts before any DDL** if `embedding_legacy_archive_0044` contains a revised
  connector's detached legacy bytes. Reconcile/export that archive explicitly;
  an automatic downgrade cannot truthfully attach an old vector to new content.
  The guard binds the RLS policy's transaction-local `bypass` sentinel itself,
  so it sees every archive row even when the migration/database owner is
  `NOSUPERUSER NOBYPASSRLS` and the table is `FORCE ROW LEVEL SECURITY`. It does
  not disable RLS or grant a persistent role attribute.

```powershell
cd backend
uv run alembic downgrade 0043_code_run_resolved_packages
```

Deploy the prior application/config and point it at the retained old OpenSearch
index. A subsequent `alembic upgrade head` restores both parked vector sets,
which is covered by the populated disposable-PostgreSQL migration test. Never
manually cast, slice, pad, truncate, or drop either vector column to recover.
Upgrade also rejects odd/ambiguous shapes (active and parked 2,048 columns
coexisting, duplicate 1,024 active+legacy columns, or a parked column of the
wrong width) transactionally. It recovers the one deterministic rehearsal shape:
legacy 1,024 + parked 2,048 with no active column.

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
