# Systematic Chrome catalog execution

Tracking: [#770](https://github.com/k-sandhu/lumen-copilot/issues/770).
Sponsor requested a narrated check-by-check Chrome pass on 2026-10-15 and
preferred an existing populated instance/account.

## Order and interpretation

`QA-WALK-001` through `QA-WALK-052` cover the 52 imported areas in their
original order. `QA-WALK-053` through `QA-WALK-071` cover the 19 proposed gaps.
These cases preserve each catalog entry's steps and expected result. They are
coverage audits: browser-visible success on one part is a partial result when
the full entry also requires API, database, multiple-role, operational or
cross-browser proof. Do not relabel the whole area as passing based on a narrow
UI observation. Existing bounded cases remain unchanged and can supply scoped
observations within this walkthrough.

Narrate the intended action before each check, then state its observed outcome
and any untested remainder. Exercise applicable UI behavior; mark unavailable
prerequisites blocked, and mechanisms outside a Chrome-only pass skipped with
an explicit reason and residual risk. Keep going through the ordered catalog.
File or link reproducible defects individually. Product fixes and merges are
outside this pass.

## Populated instance

Read-only inventory found 96 Ready documents in Eval (`eval@lumen.test`): 95
PDFs in `answer-accuracy-eval` and one TXT in an earlier QA collection. The
canonical local tenant had two Ready documents and 27 processing uploads.
The existing Eval frontend was restarted without rebuilding or resetting data;
it is served at `http://localhost:47380` with backend port 47381.

This is a composite modified development runtime. Backend source is
`3c022fcc4880228636a3cfda067c7014c89441f9` plus local gateway/tool-runner
changes in `answer-eval-643`; frontend source is
`ce4d8533b04faa2806cc09a5526f604f6ddef415` in `answer-eval`. Record both
revisions, current patch hashes, deployed image digests and selected non-secret
configuration in each run. Do not claim these results apply to clean main or
reuse earlier clean-main passes. Existing code patches are not changed by QA.

## Data and boundaries

Inspect the existing documents and histories without deleting or overwriting
them. Newly created synthetic QA objects use a unique `QA 770` prefix and can
be retained for sponsor inspection. Existing provider/connector credentials and
tenant policies are not altered. Do not enable approval-sensitive tools to make
a test pass. Missing alternate accounts, configured connectors or operational
test prerequisites must remain visible limitations.

Use the append-only ledger from [the tracking guide](README.md). Store large
screenshots and incremental observations under ignored `output/qa/770/`; commit
small sanitized run records. The source catalog and prior run files are frozen.
