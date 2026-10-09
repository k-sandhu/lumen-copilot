# ODT and RTF candidate extraction (#677)

Issue #677 specifies ordered headings, lists, tables and footnotes, declared RTF
character sets, and fail-closed malformed/deep inputs. Candidates return canonical
v1 from bytes plus package/runtime limits. They perform no I/O and have no child
document semantics. The generated tests are the acceptance corpus; no external
documents are used.

ODT reuses #711's bounded ZIP/XML reader. Require the text mimetype and the ODF
office/text/table namespaces; reject DTD/entities, encrypted entries, unsafe ZIP
names and unsupported repeated/merged/nested table shapes rather than fabricate
cells. Preserve mixed inline text, whitespace controls and XML paragraph paths.
Footnotes retain paragraph parentage. Heading levels 1..6 map to canonical roles.

RTF uses a bounded byte/control-word state machine, not conversion or execution.
Preserve paragraphs, supplied outline levels, list markers, rectangular tables
and text footnotes. Honour supported ansicpg encodings and signed UTF-16 Unicode
escapes, including surrogate pairs and scoped uc fallback counts. Reject malformed
numbers/escapes, unbalanced groups, excess depth, binary/object/image payloads and
unsupported encodings; unsupported content never becomes empty success. Formatting
controls may be ignored, but unknown starred destinations fail typed unsupported.
Source regions carry paragraph/table/footnote ordinal paths and original byte
ranges in diagnostics; canonical spans remain exact local Unicode code points.

Both input families share deadline/cancellation, accounted memory, work, output
and expansion budgets. Package limits are engineering defaults, not approved
production capacity. The optional facade requires independent default-OFF format
acceptance, candidate/shadow and cutover flags. Candidate calls do not persist,
publish or grant permissions. Python remains authoritative; live cutover requires
#687 and owner-approved baseline measurement. Upload acceptance is not widened.

Known candidate scope exclusions must return typed unsupported: RTF font-specific
charset switches, legacy codepage ambiguity, drawing/object/binary destinations,
and ODT table repeats/spans/nested tables. No claims of complete format fidelity
or production activation are made. Full baseline/RSS evaluation remains a merge
gate even when the generated acceptance corpus passes.

## Verification (2026-10-09)

- [x] Rust workspace: 41 tests passed, including seven ODT/RTF tests and generated
  Unicode/paragraph-path property cases. Initial missing-module failures preceded
  implementation; table grouping/rendered-size and footnote range regressions
  failed before their fixes. `cargo fmt --all --check`, workspace Clippy with
  warnings denied, and cargo-deny licences/advisories passed.
- [x] Rebuilt native wheel via `uv sync --extra dev --extra native
  --reinstall-package lumen-docintel`; offline bridge/configuration/conformance
  gate: 398 passed, 9 expected capability skips. Ruff and strict mypy on touched
  facade/configuration modules passed. Existing Python dispatch is unchanged.
- [~] 2026-10-09: full offline backend suite (`PYTEST_ADDOPTS=-n 2`, not-live
  selection, unique temporary root) started at 3269 MiB available and was stopped
  at 2398 MiB, below the 2500 MiB full-suite floor. No test failures appeared
  before termination; worker-loss failure is a consequence of stopping the run.
  Residual risk: complete fallback suite awaits CI.
- [~] 2026-10-09: measured baseline fidelity/RSS/throughput and cross-platform
  packaging await owner evaluation/CI. Live Postgres/OpenSearch and Docker actions
  were excluded. Residual risk: generated fixtures do not establish production
  fidelity, end-to-end ingestion or live search/publication.

| Dimension | Generated candidate evidence | Baseline evaluation |
|---|---|---|
| ODT supplied headings/lists/tables/footnotes and order | Passed | Unmeasured |
| RTF headings/lists/tables/footnotes, codepages and UTF-16 escapes | Passed | Unmeasured |
| Unicode slices and paragraph provenance | Property tests passed | Unmeasured |
| Malformed/deep/unsupported/budget input | Typed failures passed | Unmeasured |
| Python preservation, default-OFF flags and bridge detection | Passed | No live cutover |

No newly selected crates: bounded ZIP/XML, encoding and hashing dependencies are
inherited from the existing foundation/#711. The combined workspace retains
sha2 0.10.9; package generation reports that actual version. Lockfile licences
were checked, including the inherited tokenizer advisory exception.

Merge gate: hold until measured against the baseline evaluation; a human merges.
