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

Merge gate: hold until measured against the baseline evaluation; a human merges.
