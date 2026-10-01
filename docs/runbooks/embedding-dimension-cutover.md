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

There is no padding, truncation, or cross-space cast. Migration 0045 renames
the populated 1,024 column intact, adds a nullable native column, and drops only
the obsolete 1,024 pgvector HNSW index. OpenSearch remains the retrieval store.
Width alone is never treated as compatibility: every Postgres chunk and
OpenSearch document carries the vector-space fingerprint, and the index mapping
stores the same fingerprint in `_meta.lumen_embedding_space`. Query retrieval
filters to that fingerprint and then hydrates only the exact current, `ready`
Postgres ingestion attempt.

Direct media uploads use the same contract. The media worker retains its UUID
heartbeat/checkpoint lease and increments the embedding attempt in the same
claim. Paid transcription checkpoints survive embedding/index retries; transcript
chunks retain their timestamp/speaker provenance while receiving native vectors
and the target fingerprint through the attempt-owned chunk repository. Media
remains Processing through bulk publication and the search refresh acknowledgement;
only then may the current attempt publish Ready. Invalid vectors and incomplete
publication terminalize that attempt with a safe failure code, and cleanup cannot
erase another generation. Migration 0045 follows `0044_direct_media_uploads`;
`embedding_legacy_archive_0044` keeps its approved table identifier.

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
   incur model spend or poison an old index. After a failed API preflight, document
   upload, web source create, or any source resync returns a typed 503 before
   storage, upload-session/document rows, or enqueue side effects. Direct-upload
   rejections retain spec 0008's durable rejection audit evidence. Direct-upload session initiation checks collection ownership
   and input validity first, preserving 404/413/415/422 responses. Admission uses
   the request's configured vector-space fingerprint and resumes once it is valid.
   Work accepted before the failure is retried with the bounded ingestion
   backoff policy and terminalized as source `error` on exhaustion; it cannot stay
   `pending` forever. If the terminal update or commit fails, a separate Celery
   finalization retry carries the original safe reason and Ready count, skips
   preflight/fetch/ingestion, and keeps retrying until the terminal transaction
   commits (or the source was deleted). Its exponential delay is capped at the
   ordinary work budget's maximum delay; exhaustion never acknowledges a source
   that has no guaranteed recovery path. Each worker independently enforces the
   same preflight before claiming ingestion work.

   A resync that exhausts preflight before reconciliation retains its existing
   Ready documents, index entries, and indexed count. The fresh terminal
   transaction reads that count instead of resetting it to zero. If the read
   fails, terminal redrives preserve the intent to read it; if update/commit fails,
   they carry the count already read. A first source still terminalizes with zero.

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
Audio/video reservations keep the bounded `media-ingestion` queue.
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
Full web resync and managed replay without an incremental cursor also archive
every retained legacy chunk before deleting its document, even when fetched
content is unchanged. The tenant-scoped document lock serializes that archive
with ingestion replacement; archive and cascade deletion commit together.
The archive retains the original document/chunk ids, text/spans, vector bytes,
content revision, next attempt and target fingerprint. Repeating reconciliation
does not duplicate that snapshot. Explicit user deletion remains the existing
deletion operation; this preservation seam belongs to automatic reconciliation.

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
acknowledges one index-wide refresh with a positive shard total, all targeted
shards successful, and no failures. HTTP 200 with fewer successful shards than
total (even with zero failed shards) is a retryable index failure: ingestion
compensates only that attempt and persists safe `ingestion_index_error`/Failed.
The shared response validator also checks bulk item status/errors/shards and
item count, schema/alias acknowledgements, delete/update-by-query timeout/failure/
conflict signals, and complete search/count shard results. Failed provisioning
never latches success; partial cleanup never returns a completed acknowledgement.
Therefore the first search after observing
Ready must see that generation; do not use retrieval polling as cutover evidence.
Failed or superseded generations are deleted exactly by attempt where possible and are
always invalidated by the Postgres Ready/attempt/fingerprint hydration gate, so
a late worker cannot publish, delete, or fail a newer result.

The stale-work sweep also reserves its bounded age-qualified page atomically by
renewing `updated_at`, returning stale work to Pending, and clearing the old
UUID lease in the same `UPDATE ... RETURNING` that selects rows (with
`SKIP LOCKED` on PostgreSQL). Parallel poll beats therefore divide recovery work
instead of enqueueing duplicate deliveries from the same age-only snapshot.
Generic reindex cleanup is attempt-exact for every surviving document; only a
source row proven absent permits a document-wide derived-store delete. For a
Processing snapshot, repair removes only older generations and leaves the current
attempt to ingestion's publication/failure cleanup. That same attempt can become
Ready during repair's engine I/O without advancing the generation, so even an
exact-generation delete or an immediate status reread would be unsafe. Retrieval's
Ready/attempt/fingerprint gate excludes unfinished hits in the meantime. Failed
and incompatible completed snapshots still receive exact-generation cleanup.
Repair cannot delete either a same-attempt completion or a newer replacement
published while that Processing repair is in flight.

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
coordinates without re-embedding them. Migration 0045 preserves the 1,024→2,048
rollback set; it is not a general parallel store for two different 2,048 spaces,
so rolling back a later same-width model change requires the recorded backup or
a fresh backfill with the prior model.

## Rollback and rehearsal

Pause workers and stop API traffic before rollback. Migration 0045 downgrade:

- renames active `embedding vector(2048)` to `embedding_2048` (preserved);
- restores `embedding_legacy_1024` to `embedding` without changing values;
- recreates the old 1,024 HNSW index;
- removes only the new ingestion diagnostic columns; and
- **halts before any DDL** if `embedding_legacy_archive_0044` contains a revised
  connector's or full reconciliation's detached legacy bytes. Reconcile/export
  that archive explicitly;
  an automatic downgrade cannot truthfully attach an old vector to new content.
  The guard binds the RLS policy's transaction-local `bypass` sentinel itself,
  so it sees every archive row even when the migration/database owner is
  `NOSUPERUSER NOBYPASSRLS` and the table is `FORCE ROW LEVEL SECURITY`. It does
  not disable RLS or grant a persistent role attribute.

```powershell
cd backend
uv run alembic downgrade 0044_direct_media_uploads
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
