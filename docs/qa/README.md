# QA catalog and version-linked results

Tracking: [#766](https://github.com/k-sandhu/lumen-copilot/issues/766).
Accepted by the sponsor on 2026-10-15. This implements the same-repository
tracking approach requested in the current session; it changes no product behavior.

## Definitions and provenance

`catalog.json` imports the October 5 **List Existing QA Test Titles** chat:
52 existing test areas and 19 proposed coverage gaps. `source-chat-catalog.txt`
is its frozen source snapshot. IDs are permanent; retire entries rather than
reusing or deleting IDs. Areas are coverage headings, not executable test passes.
Proposed gaps remain proposed until concrete cases and evidence exist.

`cases.json` defines bounded, repeatable cases under those areas. A case records
steps, an expected result, reference docs, and an execution method. Existing
backend/frontend tests remain in their current folders. CI suite results never
implicitly mark every catalog area or manual case as passed.

## Run contract

`qa/runs/<run_id>.json` is the committed ledger of small, sanitized manual/live
records. The CLI creates files exclusively and refuses to overwrite a run.
Git history preserves later changes; CI rejects modification/deletion of existing
ledger files and removal/reuse of definition IDs. Corrections are a new run with
a new ID and an explanatory note, never an overwritten observation.

Every record includes:

- Full application commit and source state (clean, modified, or unknown); deployment
  image digests when available. The running application commit is explicitly supplied,
  never inferred from the checkout that happens to run the QA tool.
- Full test-definition commit, dirty state, and SHA-256 fingerprints of catalog
  and cases. Each case result embeds its definition snapshot and hash.
- UTC execution timestamps; environment label/kind; browser, model routes, database
  schema, fixture fingerprint and selected non-secret configuration.
- Case outcome, evidence and linked bug URLs. Partial, failed, blocked, skipped
  and not-run results carry a reason and residual risk. Passing requires evidence.
- For CI: the checked-out commit (including PR merge commits), run URL, workflow,
  attempt, suite outcomes and report fingerprint.

Use `passed`, `failed`, `partial`, `blocked`, `skipped` or `not_run`.
A fully skipped or empty suite never passes. Different commits/environments/models are
separate observations. A current definition hash mismatch is displayed as
**changed definition**; historical evidence is retained but cannot establish a
pass for the changed case. Never turn partial area coverage into a green area.

## Commands (Python 3.12+, standard library only)

From the repository root:

```powershell
python -m unittest discover -s tests -p test_qa_tracking.py -v
python scripts/qa_tracking.py validate
python scripts/qa_tracking.py catalog
python scripts/qa_tracking.py begin --run-id local-20261015-example --app-commit <full-tested-SHA> --context output/qa/context.json --output output/qa/draft.json
# Fill actual outcomes, evidence, finish time and limitations in the draft.
python scripts/qa_tracking.py record --input output/qa/draft.json
python scripts/qa_tracking.py report --app-commit <full-tested-SHA>
python scripts/qa_tracking.py report --case QA-CHAT-002
python scripts/qa_tracking.py http-smoke --run-id local-http-20261015-example --app-commit <full-tested-SHA> --context output/qa/context.json --base-url http://localhost:47180 --output output/qa/http-run.json
python scripts/qa_tracking.py record --input output/qa/http-run.json
```

The context JSON has `application` (`source_state`, `images`) and
`environment` (`label`, `kind`, `browser`, `models`, `database_schema`,
`data`, `configuration`). `begin` adds revisions/fingerprints and initializes
every case to not-run. Use only selected non-secret config, never a whole
environment dump, credentials, cookies or signed URLs. Examples are in
`context.example.json`. Image digests describe the actual deployed containers;
a branch name or mutable image tag is insufficient.

`http-smoke` is deliberately limited to loopback HTTP and unauthenticated,
non-mutating readiness/auth/input/not-found probes. Connection errors are blocked;
wrong responses fail. It does not sign in, call a model, change policies or reset
data. Other catalog cases remain explicitly not-run. Real browser checks use
the documented cases, synthetic fixtures and visible outcomes.

## CI and retention

The existing backend and frontend CI gates remain authoritative (ADR-0026).
They emit JUnit reports and sanitized suite manifests; a separate dependency-light
QA job checks catalog/ledger integrity and runs the tracking regression tests.
Always-run artifact steps preserve reports even after test failure. Report
generation cannot hide a failed test command; missing JUnit is blocked.

GitHub Actions artifacts retain raw reports and manifests for **90 days**.
The workflow summary and artifact include exact checkout/run provenance. Artifact
expiry is explicit: retain selected release reports separately before expiry.
Download artifact run JSON into a directory and pass `report --runs-dir <dir>`
to compare history. Small reviewed manual run records remain in Git; screenshots,
traces, full logs and JUnit outputs stay in ignored `output/qa/` or CI artifacts.

## Today's baseline and continued QA

The [2026-10-15 report](2026-10-15-local.md) links the first recorded local
baseline, continued Chrome cases, HTTP checks and CI evidence to their exact
versions. The JSON ledger is the evidence source; this report summarizes that
dated execution and does not represent current board status.

The [populated Eval catalog pass](2026-10-15-populated-eval.md) addresses all
71 imported areas/gaps in normal Chrome, including explicit partial, blocked
and skipped coverage. Its modified backend, frontend image and model context
are recorded separately from the clean local baseline; results do not transfer
between those environments.

The [residual coverage follow-up](777-residual-gaps.md) exercises the 39 remaining
partial-check scenarios with synthetic prerequisites, normal Chrome, and scoped
live/offline harnesses. Its separate phase ledgers retain application images,
definition revisions, configuration, failures and cleanup evidence. The
protocol golden thresholds and sequential Chrome companion are separate results;
neither silently promotes an entire catalog area or certifies another runtime.

The initial local observation predates this tracking implementation. Import it
as a retrospective observation, retaining its original timestamp, application
revision, model/fixture context and source fingerprint; do not claim the new
case definitions were executed at that earlier time. Continued QA uses the
committed definitions and records a new run.

File one reproducible bug per defect, with severity, area and affects link;
the QA pass records findings without fixing product behavior. Untested areas
remain visible with reasons and residual risk. Protected agent/contract files
are unchanged.

