# Offline extraction benchmark v1 (#670)

Generate all fixtures in code; no downloaded documents or runtime downloads.
Use `uv run --extra dev --extra native python -m tests.eval.docintel.benchmark
--report REPORT.json` from backend. The report retains every fixture in counts,
breaks down format/language/failure, records source/fixture/parser checksums and
baseline revision, timing, throughput and process peak RSS. Runtime accounted-memory diagnostics
remain separate from these process measurements.
Each parser run is isolated in a subprocess for per-document peak RSS. Output
contains metrics and safe codes, not source text. `--external PATH/manifest.json` accepts
an authorized corpus manifest with annotations; never commits its bytes.
Files above the 32 MiB in-memory benchmark profile are streamed for checksums and
reported budget failures rather than loaded wholly. Larger streaming profiles
require a landed parser and independently configured runtime limits.

Metrics for extraction arms: exact signed/numeric token-sequence fact
coverage; gold header/value/unit groups co-occurring in one rendered row/line;
ordered unique anchors (missing anchors count against the denominator); exact
half-open source-slice validation; supplied native-region coverage and explicit cell/header pairs. A missing table map scores
zero when gold requires header associations. A missing
native map scores zero for required locations, not invented page/cell identities.
Failures/empty/unsupported remain in document and metric denominators. Baseline
means the current pinned Python parsers, including their known losses. Scanned
and intentionally blank PDF fixtures are labeled separately by the generator.

The synthetic gate requires all labeled facts/associations/order and exact spans;
a dropped table, page or sheet fails its format, independent of aggregate gains.
These are deterministic engineering metrics, not accepted production thresholds.
ADR-0025 owner approval of held-out definitions/margins is still required before
promotion. Answer/retrieval evaluation remains out of this benchmark's scope.

The Rust foundation has no format parser. Its extraction candidate arms therefore
report `native_parser_not_landed`, no speedup and no promotion pass. The optional `--model-control` measurements include rendering and a bounded
Unicode executor workload with one/two/four threads, retaining accounted memory
separately from RSS. These cold end-to-end controls are separately labeled; they cannot substitute for
extraction or answer fidelity. Parser registration must add its paired corpus arm
and pass its own per-format gate before #687 can enable it.

Coverage includes valid PDFs (columns/row facts/scanned/mixed/blank), rich Office
fixtures (merged/nested tables, formulas/sparse/blank sheets, grouped slides/notes
and chart data), open containers, encodings, archives, structured text, email and
notebooks. Legacy DOC/XLS/PPT/MSG fixtures are generated CFB recognition envelopes,
not complete native-format fidelity documents; valid legacy fidelity corpora and
conversion evaluation remain gated on #678/#697. They are explicitly marked
recognition-only and never scored as successful extraction controls.

Peak RSS uses the current address-space `VmHWM` on Linux, Windows
`GetProcessMemoryInfo` and `ru_maxrss` on macOS. Linux avoids carrying a
fork/exec predecessor's rusage watermark into the fixture measurement.
These OS high-water measurements are estimates, not enforced resident-memory
limits. See the [Linux kernel proc field definitions](https://docs.kernel.org/filesystems/proc.html).

Annotated native parts verify kind/index, optional native name and a gold anchor
inside their exact character range. Duplicate, overlapping or out-of-bounds part
maps fail. A perfect count cannot compensate for a fact assigned to the wrong
page. These synthetic location checks complement exact block/chunk slices;
spatial-coordinate tolerances still require owner-approved held-out definitions.
