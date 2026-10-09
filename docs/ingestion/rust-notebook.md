# Notebook candidate (#684)

Acceptance: notebook version 4, supplied Markdown structure, intact code cells,
text outputs and binary placeholders in cell/output order. Zero-based cell and
output indices appear in source regions; source Markdown retains one-based lines.
Language comes only from notebook metadata; unknown remains unknown. Nothing runs.

JSON is preflighted for depth, memory and work before streaming deserialization.
Text output is capped at max_record_bytes on Unicode boundaries with truncation
diagnostics and a partial outcome. Partial cutover is rejected by the shared seam.
Source cells are never silently truncated; aggregate output/deadline limits fail.
NATIVE_NOTEBOOK_ENABLED and NATIVE_NOTEBOOK_SHADOW default false. New upload MIME
acceptance requires enabled cutover and a wheel exposing the notebook capability.

The foundation notebook fidelity fixture contains malformed JSON (follow-up #731).
Its failure remains reported; separate valid generated tests prove extraction.

Merge gate: hold until measured against the baseline evaluation; a human merges.

## Verification and licences (2026-10-09)

No new crates: reuses serde_json 1.0.151 (MIT OR Apache-2.0), pulldown-cmark
0.13.4 (MIT), sha2 0.11.0 (MIT OR Apache-2.0). All four cargo-deny checks pass.
Two Rust tests include Unicode offset/cell properties, depth/output budgets,
malformed JSON and inert-output checks. Clippy/all-targets, fmt and Ruff pass.
Windows wheel rebuilt/imported; 6 targeted Python tests pass.

Valid generated notebook: facts, associations, order and exact offsets 100%.
Foundation malformed fixture: typed parse rejection, scores 0% retained in the
report denominator. Python supports neither notebook fixture. Reports:
[valid](reports/notebook-valid.json), [foundation](reports/notebook.json).
Reproduce with `python -m tests.eval.docintel.notebook` and the candidate runner
`--family notebook --report ../docs/ingestion/reports/notebook.json` from backend.

[~] 2026-10-09: full backend suite deferred to CI at 2949 MiB RAM (<4000); residual
risk: broader integration regressions. Detection #728 and fixture correction
#731, stage integration #667–#669 and held-out quality/performance/RSS remain
promotion gates. Residual risk: production budget/structure/evaluation coverage.
[s] 2026-10-09: live service tests excluded by task; deployed behavior unverified.

[x] 2026-10-09: configured backend mypy passed for 6 source files.

[x] 2026-10-09: refreshed text dependency; notebook/text Rust checks pass
(10 tests). Rebuilt native wheel; 4 installed notebook/text Python checks pass.
Nested Markdown cells inherit distinct code/list units and workspace budgets.
