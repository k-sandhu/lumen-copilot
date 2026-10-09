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
