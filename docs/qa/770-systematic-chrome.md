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

During checks 1–5, the old frontend rendered the backend's inline `[S1]`
citation as plain text while its footer citation opened the correct passage.
That initial phase is preserved in its own immutable run. The frontend alone
was then replaced on the same loopback port with the already-built static image
`sha256:7a99a3e280ae0fa99e1eee9a424f08e9e598058421faf98991f061ad9e2da8f6`.
Its source is `3c022fcc4880228636a3cfda067c7014c89441f9`; a Git comparison
confirmed the frontend tree is unchanged at current main `70c3c2e4`.
The replacement container is `lumen-qa770-ui`, joined to the existing Eval
network. The old frontend container remains stopped and recoverable. Database,
object storage, existing uploads, backend patches and tenant policies were not
changed. Subsequent checks use a separate refreshed-frontend run; they do not
inherit the old client's observations.

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
