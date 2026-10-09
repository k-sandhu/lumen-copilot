# Isolated PDFium candidate — #722

Sponsor-directed acceptance contract, 2026-10-09, under ADR-0027. Python remains
live. No production promotion or password policy is introduced.

The native PDF arm uses pdfium-render 0.9.4 (MIT OR Apache-2.0), dynamically
loading the non-V8 PDFium chromium/7881 distribution (BSD-3-Clause plus bundled
permissive notices). The five platform archives and SHA-256 values are pinned in
`rust/pdfium-binaries.json`. The downloader verifies before extraction, retains
the distribution's complete licences, and never consults latest/system libraries.
Binaries are build artifacts, never source files.

Python supervises a bounded pool of execution slots, creating a fresh worker
process and PDFium instance for each document. Recycling on every document avoids
native state leakage and guarantees replacement after crash, timeout or cancellation.
Default width is min(CPU count, 2, total memory budget / per-worker cap); reject a
configuration that cannot fund one worker. Slots include startup, parsing and
IPC. No documents queue without a deadline. Windows workers enter a Job Object
with process memory and kill-on-close limits before accepting bytes. Linux and
macOS workers set RLIMIT_AS before accepting bytes; deployments must additionally
budget Celery concurrency against cgroup/container memory. Unsupported OS limits
fail closed. Parent termination is checked every 10 ms, including while waiting
for a slot or IPC. Deadlines include startup and queue time. A worker cannot run
PDFium until the limit handshake completes. Output frames are bounded before
allocation. Errors expose stable codes, never native messages or input content.

PDFium maps Unicode characters, font sizes/weights, text object identity, source
boxes and painted path segments to the existing layout/table reconstruction.
Coordinates remain unrotated PDF points, bottom-left; display rotation is retained.
Reading order and roles remain heuristic. RTL lines preserve PDFium's logical
character order; CJK has no synthetic spaces between adjacent ideographs.
Pages with no usable text or any unmapped/replacement glyphs are `needs_ocr`;
mixed documents are partial and cannot become successful native extraction.
Metadata and outline are inert supplied text. Annotation and widget appearance
text is excluded; forms, links, JavaScript and actions never execute. Encrypted
documents are rejected with a typed error, including encryption with empty passwords.

The bounded interpreter remains an explicitly selectable evaluation baseline;
there is no automatic fallback after PDFium failure. Engine selection will be
recorded after the aggregate coverage probe. The existing python/shadow/native
PDF flag remains the authority; shadow always returns Python's exact text.

Acceptance tests generate object/xref streams, incremental revisions, Type0/CID
ToUnicode maps, rotated pages, RTL/CJK, scan-only, mixed, corrupt/encrypted and
hostile inputs. Verify exact code-point spans and page/cell provenance, baseline
layout/table gold, false positives, OS limits, crash replacement, hard deadlines,
cancellation and offline wheel execution.

The opt-in corpus probe takes a directory, detects `%PDF-` by streaming, skips
files over 100 MiB, and runs at most two workers. It emits only aggregate outcome
counts, pages, characters, elapsed extraction time, peak worker RSS and pairwise
whitespace-normalized text agreement. Inputs, paths, names, content and hashes
never enter reports or logs. Failed documents remain in coverage denominators.

Merge gate: hold until measured against the baseline evaluation; a human merges.
Owner decisions remain production budgets, cutover thresholds, OCR, password
policy and acceptance of annotation exclusion/heuristic reading order.
