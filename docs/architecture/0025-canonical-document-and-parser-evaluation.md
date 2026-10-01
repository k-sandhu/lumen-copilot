# ADR-0025 — Canonical document, provenance and structured-parser evaluation

Status: **Proposed — design only; adoption requires owner evaluation and a subsequent implementation issue.**

Date: 2026-09-30. Tracking: [#633](https://github.com/k-sandhu/lumen-copilot/issues/633).
Related: #621 (native locations), #628 (inspection), #632 (fingerprints).

## Context

Flattening enterprise documents loses reading order, table relationships,
heading hierarchy and native locations. Native parsers remain a useful small,
offline baseline. A structured parser may improve these properties but also
change text, offsets, dependencies, resource use and failure modes. Empty native
PDF text does not establish whether a page is scanned or intentionally blank.
Parser names and confidence scores do not establish answer accuracy.

The owner must measure the evaluation baseline before any related PR merges.
This ADR introduces no runtime, dependency, OCR engine, migration, wire contract
or deployment change. It does not choose a production parser or OCR engine.

## Proposed design

### Canonical representation and exact citations

Keep an immutable extraction generation separate from the mutable document
lifecycle. Its domain model contains ordered blocks and explicit relationships:

| Element | Required semantics |
|---|---|
| Paragraph/list/heading | Stable block ID, reading order, parent/children, text and heading path; distinguish supplied heading roles from heuristics |
| Table | Row/column grid, blank positions, cells with row/column spans, explicit header roles or unknown, caption and units as supplied |
| Workbook | Named sheet, native row/column coordinates, values and formats/units; distinguish formulas from cached results and unknown cache freshness |
| Presentation | Slide number/title, group order, table grid and separately labeled speaker notes |
| Page/figure | Native page and optional bounding box with coordinate origin/unit; original caption separate from any generated description |
| Furniture | Headers/footers separate from body order; retain provenance even when excluded from retrieval |

Missing structure stays unknown. A first-row header guess is marked heuristic.
Do not infer a value's unit or calculate a workbook formula during extraction.
Generated figure descriptions, summaries and OCR corrections are derived
content with separate lineage; they never masquerade as original evidence.

A deterministic, versioned renderer produces the canonical extracted text and
a block-to-text span map. Every persisted chunk has an exact half-open range:
`source[char_start:char_end] == chunk.text`. Offsets count Unicode code points
in the retained source, not byte positions; client conversions must be explicit.
Original-document page boxes and cell coordinates are a second provenance
system, never substituted for linear text offsets. A multi-page table carries
all contributing native regions and preserves the renderer's reading order.

Headings, repeated table headers and parent context may accompany a passage as
metadata or an explicitly derived embedding/context serialization. That wrapper
does not become the quoted evidence text. If a derived serialization is indexed,
persist its mapping back to the canonical block ranges; never assign canonical
offsets to duplicated header/prefix text. Citation resolution selects the
retained generation and the canonical slice, with current permission checks.

### Hierarchy and tokenizer-aware chunks

Evaluate parent-child chunks after evaluating extraction alone. Children should
retain heading paths and native regions; parent expansion remains permissioned
and budgeted. Freeze the embedding tokenizer/model and maximum token budget for
each evaluation arm. Oversized blocks/tables require bounded child splitting,
stable cell/header relationships and explicit overlap behavior. Count the full
embedding/context serialization against its budget, including header metadata.
Do not let character size silently stand in for an embedding model token limit.

### Adapter and generation provenance

Keep native and structured adapters within `backend/app/ingestion/`, behind one
domain-returning interface. Lazily import format/provider dependencies inside
their adapter helpers (ADR-0004). Expose canonical domain objects, typed parse
outcomes and diagnostics; vendor/Pydantic parser objects do not leave the adapter.
Do not introduce a second database, retrieval store or parser HTTP service by
default. A future remote parser is a separate external boundary and ADR.

Each generation records source SHA-256, extraction ID, parser/build/dependency
versions, renderer/schema version, chunker settings/tokenizer, actual embedding
model/dimension and optional OCR engine/model/language/build identities. Retain
original bytes and optional canonical artifacts through the existing tenant
object-storage boundary; persist artifact references and checksums, not secrets.
Persist source, maps, chunks and provenance atomically through tenant-scoped
repositories. Publish searchable readiness only after successful refreshed
synchronization with the existing OpenSearch store (ADR-0010).

Re-ingestion creates a new generation from original bytes; re-indexing republishes
existing chunks without reparsing. A parser upgrade never rewrites old citation
offsets in place. Historical citations resolve their retained generation, or
explicitly report it unavailable/stale under the chosen retention policy. They
must not silently retarget an old offset to newly rendered text. Retention,
migration/backfill and historical-source API policy need owner decisions before
implementation. A current permission revocation applies to historical evidence.

## Structured-parser evaluation

[Docling's document model](https://docling-project.github.io/docling/concepts/docling_document/)
represents ordered hierarchy, text, tables, pictures, furniture and optional
layout/provenance. It is a candidate adapter, not this application's domain schema.
[Its chunkers](https://docling-project.github.io/docling/concepts/chunking/) include
hierarchical and tokenizer-aware hybrid processing with table-header handling.
These capabilities motivate experiments; they are not measured accuracy claims.

Build a checksum-fixed corpus of approximately 40–60 small synthetic or explicitly
authorized documents, stratified by native/scanned PDF, mixed pages, columns,
headings/furniture, sidebars/footnotes/captions, merged and multi-page tables,
sparse/formula/unit workbooks, grouped slides/notes, non-ASCII text,
malformed/unsupported and intentionally
blank files. No third-party downloaded documents enter the repository. Separate
tuning and held-out fixtures/questions. Label reading order, important cells,
units, native regions, gold answers and acceptable evidence spans; include
unanswerable questions and permission-denied evidence.

1. Measure the frozen native baseline, including failures in the denominator.
2. Compare native and pinned Docling extraction with the **same downstream
   renderer/chunker, embeddings, index, retrieval and chat contract** where the
   representations permit it. Record any unavoidable renderer difference.
3. Evaluate structural/tokenizer chunking in a separate arm after extraction;
   do not attribute its gains to the parser. Evaluate OCR separately, page by page.
4. Replay held-out questions and permission negatives under the same settings.

Report extraction success/empty/partial/failure by format, reading-order and
cell/header/unit correctness, page coverage, exact slice/provenance validity,
retrieval recall/rank, grounded answer correctness, supported abstention,
citation correctness, latency and resource cost. Heuristic native diagnostics
are inspection signals, not ground-truth fidelity scores. Keep answerability
and faithful refusal separate so fabricated answers cannot improve the score.

Hard gates: exact slice validation for every produced chunk; tenant/permission
negatives remain denied; no forbidden citation, missing required audit or
untyped parse failure. Owner-defined thresholds must assess native text and
existing answers as well as the target table/spreadsheet/scanned cases. Publish
paired results with format-level failures and costs before an adoption decision.

### Connector exports and inspection replay

Compare authorized representations of the same connector item before choosing
an export format: native Office/PDF, structured export and flattened text/CSV
where supplied. Freeze the source revision, permissions and downstream settings;
record connector/export identities and checksums separately from parser identity.
Score lost tables, units, formulas, notes and reading order, including two-column
flow and repeated furniture. Preserve genuine repeated body text when evaluating
header/footer exclusion. An export choice needs measured fidelity and cost;
this proposal changes no connector or export policy.

A future administrator inspector should align the original page/slide/sheet,
ordered canonical blocks, rendered text, exact chunk ranges, provenance and
diagnostic warnings for one extraction generation. Show unknown geometry and
heuristic relationships explicitly. A permissioned replay with another pinned
parser profile produces a separate candidate generation with its own diagnostics
and fingerprint; it does not overwrite active evidence or silently retarget
citations. Record committed inspection/replay audit events and include foreign
tenant and unauthorized-document negatives. Promotion requires the agreed
evaluation gates; replay authorization, storage/retention and promotion UX need
their own contract-first implementation issue. The current native diagnostic
summary is a first inspection step, not this full comparison/replay surface.

## Future OCR options and placement

| Candidate | Deployment implications to measure |
|---|---|
| [Tesseract](https://github.com/tesseract-ocr/tesseract) | Apache-2.0 engine; native executable/library, Leptonica and selected language traineddata; benchmark segmentation/languages and image preprocessing |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | Apache-2.0 project; choose/pin an inference backend and model artifacts; measure CPU ONNX-runtime option before any accelerator profile |
| [EasyOCR](https://github.com/JaidedAI/EasyOCR) | Apache-2.0 project; PyTorch/torchvision and language-specific model artifacts; measure dependency footprint, startup and memory |

Verify pinned engine, model and transitive artifact licenses independently; a
project's code license does not license every weight or language dataset.
[Docling's OCR options](https://github.com/docling-project/docling/blob/main/docs/concepts/OCR.md)
and [offline artifact configuration](https://docling-project.github.io/docling/usage/advanced_options/)
provide candidate integration paths. Prefetch checksum-pinned artifacts during
a controlled build; disable runtime downloads/network access in the worker.

OCR belongs in a future isolated Celery worker profile, outside API requests,
with bounded pages, archive expansion, runtime, memory and concurrency. Start
with CPU benchmarks; a GPU profile needs separate measured justification.
Use selective native-region/page fallback only after an evaluated policy can
distinguish intentional blanks from missing evidence; native and OCR text need
explicit region lineage and duplicate handling. Language selection, fallback
thresholds, retries and maximum resource budgets remain owner decisions.
Remote/cloud OCR is outside this proposal and requires its own adapter, privacy,
cost and egress decision. No external bytes are sent anywhere in this session.

Measure incremental compressed/uncompressed worker image size, dependency and
model artifact sizes, offline build reproducibility, cold/warm startup, per-page
CPU time, peak RAM, throughput and quality by language/format. Report resource
cost as measured CPU/GPU time multiplied by supplied infrastructure rates, plus
artifact storage/transfer; do not invent image-size estimates or currency costs.
Isolation and limits must turn corrupt/hostile bytes into typed outcomes and
committed audit records rather than worker crashes or cross-tenant artifacts.

## Consequences and remaining decisions

This design preserves exact citations while enabling structural retrieval, but
adds renderer/mapping complexity and versioned storage cost. Structured parsing
and OCR increase worker footprint and require adversarial fixtures and resource
budgets. Native parsers remain the fallback until measured adoption is approved.

Owner decisions: held-out corpus and release thresholds; accepted parser/renderer
and OCR engine/languages; CPU/GPU budgets and deployment profile; generation
retention and historical citation resolution; backfill sequencing; whether
derived figure descriptions enter retrieval and under what evidence policy.
Those decisions belong in follow-up implementation issues and contract-first
changes. This proposed ADR closes none of them by implication.
