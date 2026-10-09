# Selective hosted OCR — issue #695

Decision and acceptance contract: [ADR-0029](../architecture/0029-selective-hosted-ocr.md).
This is an opt-in candidate behind the existing native ingestion foundation;
the draft merge remains held for baseline evaluation and human acceptance.

## Enablement and consent

Install the optional backend `native` extra and the packaged PDFium engine.
Deployment configuration stays in Settings: `OCR_ENABLED` defaults false,
`OCR_MODEL` defaults empty, and `OPENROUTER_API_KEY` supplies the hosted key.
A deployment switch or a configured key never enables a tenant.

Before operator provisioning, an administrator must explicitly approve pages
leaving the tenant boundary for OpenRouter, the parser and the chat provider.
Regional processing, retention and provider terms require owner acceptance.
There is no new tenant admin UI or unauthenticated configuration endpoint.
The trusted provisioning service checks that the approving user is an admin
in that tenant and emits the settings audit in the caller's transaction:

```python
async with tenant_session_scope(tenant_id) as session:
    await configure_ocr_policy(
        OcrRepository(session, tenant_id),
        AuditSink(AuditEventRepository(session, tenant_id)),
        OcrPolicy(
            tenant_id=tenant_id,
            enabled=True,
            approved_by=approving_admin_id,
            page_limit=approved_page_limit,
            budget_usd=approved_total_usd,
            per_page_ceiling_usd=approved_total_page_ceiling_usd,
            concurrency=1,
        ),
    )
```

The example uses domain types from `app.domain.ocr`, the repository from
`app.db.ocr`, and `configure_ocr_policy` from `app.services.ocr_policy`.
Use positive owner-approved budgets and a cheap restricted chat model/key.
The parser is documented at $0.002/page ($0.0022 US), with chat billed separately.
Local reservation ceilings cannot force the provider's price; unexpected actual
costs are recorded and block publication. Unknown costs retain the reservation.
Counters are lifetime totals; changing policy does not reset spent pages/cost.

## Processing and recovery

Only canonical pages flagged `needs_ocr` are selected. PDFium copies each page
inside the existing OS-limited worker; PNG/JPEG/WEBP/single-frame TIFF become
one-page PDFs there. Extra TIFF frames fail explicitly. Generated PDFium wrapper
IDs and creation dates are stable, so the submitted content hash remains stable
across ingestion retries. Original source bytes are unchanged.

The provider owns all OpenRouter request/annotation shapes. It reads only file
annotation text, including annotations returned when chat inference fails;
exact separate parser envelope parts are removed without normalizing recognized
text. No boxes or confidence are invented. Incomplete OCR checkpoints are re-evaluated on retry so a temporary occupied
slot cannot permanently mask newly completed page cache entries. Complete
checkpoints remain reusable. The canonical OCR origin, engine,
nullable confidence, page regions and exact Unicode code-point spans are saved
in the `ocr` checkpoint between extraction and normalization. Chunks overlapping
OCR evidence carry the machine-read marker into streamed and stored citations.

The tenant-bound page cache and cost reservation commit an audit intent before
network dispatch. Completed pages are reused without another charge even if
embedding/indexing or a downstream checkpoint fails. No automatic paid retries
or provider fallback occur. A timeout/error with uncertain billing consumes its
reservation; a crash before settlement keeps a pending cache entry and tenant
slot. Operators must reconcile ambiguous requests before releasing held slots;
never delete pending entries merely to force another paid attempt. Cache data
is protected by tenant RLS and removed on tenant deletion; retention periods
and reconciliation tooling remain owner decisions.

When OCR is off, unconfigured, incomplete, over budget or unavailable, ingestion
records `needs_ocr` and stops before embedding/index publication. A scan never
becomes a successful empty document. Machine-read citations communicate that
OCR may contain recognition errors.

## Verification record

- 2026-10-09: Rust workspace tests, Clippy with warnings denied and cargo-deny
  license/advisory/source gates passed. Unicode property tests include combining
  marks and supplementary code points; generated images cover all four codecs.
- 2026-10-09: 114 focused offline tests passed, including native single-worker
  preprocessing, byte stability across clock seconds, downstream-fault replay,
  tenant/budget/concurrency denials, missing-audit rollback, migration DDL and
  machine-read citation/redaction projection. No live datastore was contacted.
- 2026-10-09: TypeScript checks and 18 chat/viewer integration tests passed;
  machine-read labels survive streamed evidence and reloaded history.
- 2026-10-09: one generated-page live request returned HTTP 429 with recoverable
  parser annotations; the generated amount 123.45 was recognized. A sanitized
  fixture is committed. Usage/cost was absent: no exact billing receipt is
  claimed. The probe routing cap/context bound was below $0.016, within the
  authorized $0.02 limit. No paid retry or second request was made.
- 2026-10-09: full backend run with `PYTEST_ADDOPTS="-n 2"`, `RUN_LIVE=0`
  and `pytest -m "not live" -k "not live and not two_process_slots"` passed:
  3695 passed, 71 skipped, one existing xfail (#421). The optional extension was
  removed for this broad run; its five OCR worker tests passed separately.
  The final native-unavailability/counter-width fixes additionally passed 125
  focused ingestion/provider/accounting/migration tests (three live cases
  deselected). The final complete/incomplete checkpoint reuse regression run
  passed 132 tests, including six native OCR cases with one worker. It proves
  recovery after a concurrency slot becomes available as well as completed
  checkpoint/page reuse after an embedding fault. Three inherited unmarked RLS cases probed unreachable localhost
  before skipping; no database was created/modified. Follow-up #749 tracks the
  gate. Future offline runs must also exclude `tests/test_rls.py` until fixed.
- 2026-10-09: ruff lint/format and mypy (212 app files) passed. Frontend lint
  passed. Compose structure passed with `docker compose --env-file .env.example
  config --quiet --no-env-resolution`; no containers were started/stopped.
- [~] 2026-10-09: latest-head CI is pending after final defensive fixes; previous
  implementation-head backend/frontend, all five native platform jobs and
  Docker packaging passed. Residual risk is latest-head integration coverage
  until the new CI run completes.
- [~] 2026-10-09: live Postgres/OpenSearch execution and local container actions
  are excluded by the task; real RLS/locking and live indexing behavior remain
  unverified (the inherited localhost probe/skip gap is tracked separately above).
- [~] 2026-10-09: baseline OCR quality evaluation and provider consent/terms are
  human merge/rollout gates; a generated-page probe is not an accuracy benchmark.

Migration 0049 depends on the stage foundation's 0047. The parallel classification
stack reserves 0048; reconcile migration heads when the draft stacks converge.
Self-hosted OCR remains #696 behind the same domain interface, and PDFium budget
coverage/calibration remains #739.
