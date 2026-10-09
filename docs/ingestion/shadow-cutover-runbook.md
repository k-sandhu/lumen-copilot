# Ingestion shadow and cutover operator runbook — #687

Depends on #726 and #751. Keep Python parsers installed. Defaults are Python for
all formats. Configure independent modes with JSON, for example:
`INGESTION_FORMAT_MODES={"pdf":"shadow","docx":"python"}`.
Only PDFium is landed in this dependency stack. Other format adapters remain in
their own PRs; unlanded native candidates fail closed. Restart workers after any
configuration change. Mode/build/budget identity invalidates extraction checkpoints.

Use a valid local access-token file for a current administrator; tenant comes
only from the verified token. Current ownership/grants/mirrored ACL still govern
document reads, including diagnostics. A tenant administrator has no read wildcard.
Never commit the token file. No command below was run against a live datastore.

```text
uv run --extra dev python -m app.ingestion.cutover report --token-file <local-token-file> --pages 10
uv run --extra dev python -m app.ingestion.cutover report --token-file <local-token-file> --document <uuid> --diagnostics
uv run --extra dev python -m app.ingestion.cutover preview --token-file <local-token-file> --collection <uuid>
uv run --extra dev python -m app.ingestion.cutover preview --token-file <local-token-file> --document <uuid>
uv run --extra dev python -m app.ingestion.cutover preview --token-file <local-token-file>
uv run --extra dev python -m app.ingestion.cutover replay-originals --token-file <local-token-file> --collection <uuid>
```

Each page is bounded to 100; `--pages` bounds total work and a returned cursor
resumes remaining inventory. Replay reads stored originals through ObjectStore,
recomputes baseline/candidate comparison, and writes only diagnostics plus audit.
It does not replace chunks, enqueue ordinary ingestion or alter old citations.
The explicit `execute-generation` command rejects before bytes/writes while the
[immutable generation proposal](reingestion-generation-proposal.md) is awaiting
owner policy decisions. This is a deliberate implementation boundary, not a claim
that generation activation/backfill has shipped.

Reports summarize comparison records per format/outcome, exact matches and
positional code-point mismatches. Counts include distinct profile samples of a
document; the first observation for each original/parser/budget fingerprint is
retained. Retrying the same profile does not replace that observation or inflate
counts; select a new profile to measure a changed build or budget. These metrics are diagnostic,
not edit distance, extraction fidelity, retrieval or grounded-answer evaluation.
A safe `ingestion.shadow_diagnostic_unrecorded` counter warns that persistence
failed while the live baseline continued; investigate before treating report
coverage as complete. Failed Python baselines are recorded with `baseline_failed`;
their original error is retained and equality is never claimed. Mismatch counts
are zero when no baseline exists, not a claim of equality.

After owner baseline evaluation approval, set a selected format to `native` only
for fresh documents with no existing evidence. Incomplete/failed candidates fail
closed. Existing evidence is refused until immutable generation policy is frozen.
Rollback: return that format to `python`, restart workers, and retry affected
fresh documents. Other formats retain their modes. A permanent publication fence
prevents later automatic replacement of native evidence by any route, including
rollback to Python. Unpublished native index failures may resume verified
checkpoints when no citations refer to the work. Parser removal is later work.

Migration 0048 depends on #726's 0047. Coordinate sibling draft migrations against
origin/main 0046 before merge. Deployment/RLS/index validation remains a live gate.

Merge gate: hold until measured against the baseline evaluation; a human merges.
