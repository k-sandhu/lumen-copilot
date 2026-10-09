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
Neither global defaults nor the evaluation profile are raised to hide failures.

Merge gate: hold until measured against the baseline evaluation; a human merges.
