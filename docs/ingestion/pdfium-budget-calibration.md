# PDFium budget calibration — #739

Acceptance follows #739 and ADR-0027: identify every budget failure with safe
numeric counters; charge retained memory separately from page scratch; measure
complete-document coverage against the identical pypdf profile; preserve all
input, output, work, deadline, structure and OS memory limits. Production budgets
and cutover thresholds remain owner decisions. No API/WS contract changes.

Failure telemetry is allowlisted numeric page/glyph/work/output/input/peak-accounted
counters plus a stable limit category, passed over bounded worker IPC. The local
probe reduces these to p50/p95/max by engine and failure category. No per-document
identity, content, name, path or hash is retained. Python-complete/native-incomplete
gaps are counted by outcome or failure category; failures stay in denominators.

Generated regressions first reproduced missing failure telemetry and a 100-page
document failing the 256 MiB accounting budget while within its output/work caps.
Calibration measurements and final verification will be recorded in the draft PR.

Instrumented baseline: 64 PDFs, PDFium 29 complete / 12 OCR or partial / 2
encrypted / 21 failures; pypdf 52 complete. Of 19 native budget failures, 18 hit
accounted memory (p50 peak 268,435,065 bytes), with p50 456,395 visited glyphs.
Their p95 work was only 1,038,791 of 5,000,000 units. The other budget failure
hit an algorithmic structure bound; it is now explicitly attributed.

The corrected path drops glyph/char-box/layout reservations after each page,
retains and charges canonical block/cell/heading metadata, and charges original
bytes once rather than again as nonexistent serialized copies. Per-glyph work
is retained: extraction, layout and table scans really do visit those glyphs.
Global default document budgets remain unchanged. The calibration evaluation
profile is separately measured and explicitly supplied to every engine.

Merge gate: hold until measured against the baseline evaluation; a human merges.

The first corrected run at unchanged 5M work units yielded 35 complete PDFs,
14 OCR/partial, two encrypted and 13 failures. Remaining limits were memory=1,
work=5, structure=5. Work failures reached roughly 1M glyphs at 5M work units,
while the output ceiling is 2M characters. The proposed evaluation ceiling is
20M work units: 10M for the five measured glyph passes, up to 4.2M for bounded
ruling comparisons, plus hashing/serialization/table overhead and headroom.
This is an evaluation profile, not a production capacity decision.

The ruling ceiling is 2,048 segments (previously 512); comparisons remain charged
quadratically before work and the 10,000-cell ceiling remains. Generated 600-line
input failed under the prior ceiling; 2,100 lines must still fail closed. The
probe records maximum page rulings to validate this hypothesis against measured
distributions. JSON is counted without allocation, then its measured growth is
reserved before serialization. The 32 MiB canonical JSON ceiling remains.

The sparse-table regression previously assumed serialization always costs more
than peak extraction scratch. After scratch release that assumption is false;
it now verifies measured JSON remains accounted and a sub-JSON memory budget
still fails closed. No hostile-input guard is removed.

## Final aggregate result (2026-10-09)

64 PDFs detected by magic; zero oversized or unreadable. At most two workers.
Final native-only confirmation after releasing renderer scratch: 38 complete,
15 OCR/partial, two encrypted, nine failures, zero timeouts. Failures: output=3,
table-rulings=2, parse=3, invalid-structure=1. Memory and work failures are zero.
The five remaining budget failures are all attributed. 1,501 returned pages,
6,538,240 characters, 46.06 seconds, 237.61 MiB peak worker RSS. Final returned
PDFium distributions: accounted-memory p95 107.46 MiB / max 221.98 MiB;
work p95 8,307,117 / max 12,102,941; page rulings p95 456 / max 996.
The calibrated ruling ceiling is approximately twice observed successful max;
the work ceiling leaves headroom above observed maximum and keeps a hard bound.

The full three-engine run immediately before that final scratch fix returned
pypdf 51 complete / 6 OCR / 2 encrypted / 3 memory failures / 2 timeouts;
in-core 1 complete / 4 OCR / 52 unsupported / 7 parse failures. Frozen pypdf
baseline and both prior reruns were 52 complete with one timeout. Deadline
variance is explicit. The accounting-only native rerun recovered one memory
failure as an OCR/partial document; complete-document coverage remained 38.

The paired run identified Python-complete/native-incomplete gaps: eight OCR or
partial outcomes, three parse failures, one invalid canonical structure and one
memory failure. The final accounting-only rerun eliminated that memory failure
and returned one additional OCR/partial outcome. OCR diagnostics distinguish
unusable glyph mappings, image-only and blank/empty pages; source text was not
inspected or emitted. Parse/geometry validation stays fail-closed. Remaining
budget cases exceed output or ruling ceilings (not parity successes in the
paired Python arm). No ground-truth fidelity or coverage parity is claimed.

- [x] Generated failure telemetry and long-document regressions passed; ruling
  regression first failed under the old ceiling, then accepted 600 and rejected
  2,100. Hostile forms, memory caps/deadlines and exact evidence tests passed.
- [x] Rust workspace: 37 tests passed. Focused Python PDF/runtime checks: 42 passed.
  Scoped Ruff formatting/lint and strict mypy passed.
- [x] Full offline backend CI at calibration commit 69ef3aa: 3,656 passed,
  190 skipped, one xfailed; lint/format and mypy passed. Five-platform native,
  installed-wheel, fidelity and CI Docker packaging gates passed at that commit.
- [~] 2026-10-09: final renderer-scratch delta and aggregate artifacts await final
  CI; residual risk: broad/platform regression coverage at final head pending.
- [~] 2026-10-09: live Postgres/OpenSearch and local Docker excluded by user;
  residual risk: deployment integration and held-out fidelity remain unverified.

Complete coverage still trails the frozen Python baseline. The owner must review
strict OCR handling and valid-input parse/geometry gaps against held-out fidelity
before promotion; do not reinterpret these as successful extraction. Production
budgets remain an owner decision. This PR stays draft and the issue stays open.
