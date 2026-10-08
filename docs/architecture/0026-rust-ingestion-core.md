# ADR-0026 — Rust ingestion core behind the Python backend

Status: **Proposed — sponsor-directed foundation; human acceptance and baseline evaluation required before merge.**

Date: 2026-10-08. Tracking: [#662](https://github.com/k-sandhu/lumen-copilot/issues/662), epic [#661](https://github.com/k-sandhu/lumen-copilot/issues/661).
Builds on [ADR-0025](0025-canonical-document-and-parser-evaluation.md), [ADR-0004](0004-architecture-boundaries-and-adapters.md) and [spec 0004](../specs/0004-security-and-domain-invariants.md).
Amends ADR-0003's Python-only computation choice; the accepted historical ADR is retained unchanged.

## Context

State-of-the-art enterprise document ingestion needs faithful reading order,
tables, formulas, notes and exact citations across document families. Python
already provides a useful offline native baseline. CPU and allocation intensive
work needs bounded parallel execution without moving trust or storage boundaries.
The foundation does not select a production format parser, OCR engine, retention
policy or answer-quality threshold. ADR-0025's outstanding decisions remain open.

## Decision

### Ownership and boundary

Python remains the only HTTP backend. API/WS, authentication, tenant identity,
permission enforcement, Celery orchestration/retries/leases, audit persistence,
network access, object storage, relational writes, model calls, embeddings and
OpenSearch publication MUST NOT move to Rust in this phase. Rust receives only
bytes or bounded segments already obtained by Python and inert configuration.
It never receives credentials, database handles, storage keys as capabilities,
or authorization authority. Media transcription remains under ADR-0023.

Rust owns detection, decoding, parsing, canonical structure, normalization,
chunking and later classification feature computation. No second service or
retrieval store is introduced. The sole Python import chokepoint is
`backend/app/ingestion/native.py`; imports are lazy and optional. Its callers
receive Python domain objects, never PyO3 internals. Existing Python parsing
continues without the extension. The foundation adds no production cutover.

```
rust/Cargo.toml                         workspace, resolver and shared versions
rust/crates/lumen-docintel/             pure Rust computation/domain model
rust/crates/lumen-docintel-py/          PyO3 cdylib named lumen_docintel
backend/app/ingestion/native.py         optional domain-returning facade
```

The core has no Python types and no network/storage dependencies. The bridge
owns argument conversion, GIL release, panic containment and typed exceptions.
Freeze a versioned JSON representation before model implementation (#664);
Python exposes immutable typed records and serialization uses that same shape.
Ordered blocks include headings, paragraphs/lists, tables/cells/spans, figures,
captions, footnotes, code and furniture. Parent relationships, supplied versus
heuristic roles, page/slide/sheet/cell locations and optional boxes with explicit
units/origin remain distinct from linear offsets. Unknown remains unknown.

Rendering is deterministic and versioned. Every block span satisfies
`rendered_text[char_start:char_end] == block_text` using Unicode code points,
not UTF-8 bytes, UTF-16 units or grapheme clusters. Derived labels/context never
borrow evidence offsets. Preserve generation/source hashes, parser/build and
renderer/schema identities, diagnostics/outcomes, tokenizer/chunker identities,
embedding model/dimension and optional OCR lineage. Align additively with the
open provenance (#625), outcomes (#630), fingerprint (#640) and readiness (#629)
PRs without modifying their branches. Rust cannot activate a generation: Python
continues atomic tenant-bound persistence and refreshed search-readiness gates.

### Parallelism, resource limits and failure containment

Use a bounded, explicitly constructed rayon pool, not the global pool. Preserve
input order while computing independent documents or page/sheet/slide/member
units in parallel. Cap both pool threads and in-flight documents; avoid nested
pool multiplication with Celery. Long bridge calls release the GIL, including
serialization/validation over large documents. A Python-thread/native-thread
handshake proves release without a speed or sleep assertion.

Each document has configuration-supplied input, expanded archive, output,
work-unit, elapsed-time and memory accounting limits. Initial engineering test
profile: 32 MiB input, 128 MiB accounted intermediate/output memory, 2 million
output code points, 100,000 work units, 30 seconds, two threads and one in-flight
document per Celery child. These are conservative test defaults, not approved
production capacities. Charge allocations/expansion BEFORE doing work; stream
large input segments through Python rather than duplicating the whole source.
Check a shared cancellation token/deadline at bounded work boundaries (target
check interval <= 10 ms or one bounded unit; cancellation return <= 100 ms in
cooperative test workloads). No partial result is successful extraction: return
a typed failed/partial outcome and explicit incomplete regions.

Cooperative limits do not bound an uninstrumented dependency or process RSS.
Parser adoption requires bounded dependency operations; a non-cooperative or
unsafe native engine needs an isolated worker process with a hard supervisor
limit before production use. Thread cancellation cannot kill native code.
Celery process concurrency times per-process pool width must fit the configured
CPU/memory allocation. Python owns termination and retry policy; the Rust pool
must not starve healthy documents behind an unbounded unit.

All fallible core operations return a domain error (invalid structure/input,
unsupported format, decode/parse failure, memory/work/time budget, cancellation,
internal fault). Map each category to a named Python subclass of DocIntelError
with stable safe messages/codes. Wrap EVERY computational bridge entry in
`catch_unwind`, build with `panic = "unwind"`, and translate panic to
DocIntelPanicError without payload/source disclosure. A test injects a panic,
then succeeds in the same worker process. Abort/OOM/segfaults are not catchable;
pre-allocation checks and isolation remain necessary. Never log document bytes.

### Build and release

Pin Rust 1.99 stable, Cargo.lock, direct crate versions, PyO3 and maturin.
Use PyO3's Python 3.12 stable ABI where supported. The bridge is a separately
packaged local dependency, optional through a backend `native` extra and uv
path-source configuration, leaving Python-only installs operational.

One-command developer build from repo root:
`uv sync --project backend --extra dev --extra native` (reinstall/rebuild the
local extension after Rust edits as documented in #663). Windows uses the
MSVC host plus Visual Studio Build Tools; put `%USERPROFILE%\.cargo\bin` on
PATH. macOS needs Xcode command-line tools; Linux needs a C linker/build tools.
Keep Cargo output outside synced folders. On this Windows host retain
`CARGO_TARGET_DIR=C:\Users\sandh\AppData\Local\cargo-target\lumen` and
`CARGO_BUILD_JOBS=2`. Wheels are platform/architecture specific; never copy a
Windows build into Linux. Release uses maturin-built wheels for supported
Windows/macOS/Linux architectures and import checks on each target; no runtime
compiler is required.

Docker builds from the repo root with a Python 3.12 + pinned Rust builder stage,
produces a wheel, and copies only the wheel into the existing backend runtime.
API and Celery workers share that image; Rust/compiler/Cargo caches stay out of
it. Compose build context changes must preserve source mounts and entrypoints.
Measure image size and elapsed build time; a local Docker build requires at
least 6000 MiB available RAM. Do not start/stop containers for this foundation.

Scoped Rust CI in #663 is explicitly authorized by this task, without deciding
the repo-wide OD-7 pipeline: shared Cargo/uv caches; `cargo fmt --check`,
`cargo clippy --workspace --all-targets -- -D warnings`, `cargo test --workspace`,
`cargo deny check licenses advisories`, maturin build and Python import/bridge
smoke on Windows/macOS/Linux. Tests are offline, with no provider/database/search
access. Cache keys include toolchain, platform, lockfile and Python ABI. Publish
build timings and artifact sizes rather than guessing them.

### Dependency and license policy

Only explicitly allowlisted permissive SPDX licenses: MIT, Apache-2.0,
BSD-2-Clause, BSD-3-Clause, ISC, Zlib, Unicode-3.0 and similarly reviewed
permissive terms. No GPL, AGPL, LGPL, unknown licenses or unreviewed git sources.
MuPDF bindings are excluded. Check transitive crates and native binaries/data
independently; a wrapper's permissive license cannot license its bundled engine.
Use cargo-deny for licenses/advisories, deny unmaintained/yanked dependencies
where supported, record any reviewed advisory exception with expiry and owner.
Every PR lists newly introduced direct dependencies and licenses; Cargo.lock and
cargo-deny cover the transitive graph. Candidate crates (not adopted here):
rayon/PyO3/serde, quick-xml/zip, encoding_rs/chardetng/infer, calamine,
lopdf or separately reviewed BSD-3 PDFium, html5ever/scraper and tokenizers.

### Migration and measurement gates

Parity first. #670 freezes generated fixtures and the improved Python baseline
at a commit, with failures in denominators. #687 introduces opt-in shadow mode:
Python remains authoritative, native errors never alter its live result, safe
per-format differences are recorded without document content. Independent
per-format enable/rollback flags require the owner-approved fidelity evaluation.
Re-ingestion starts from original bytes and creates a new immutable generation;
re-indexing reuses chunks. Never rewrite old citation offsets or silently retarget
a retained generation. Remove a Python parser only after its format passes and
rollback/extension availability policy is approved; new formats stay gated until
a parser lands. An unavailable parser returns typed unsupported, not empty success.

Engineering performance targets (hypotheses to measure, not achieved claims):

| Document family | Throughput per core relative to frozen Python baseline | Peak incremental RSS target per document |
|---|---|---|
| Text/Markdown/HTML/structured text | >= 2x decoded MiB/s | <= 64 MiB at <= 8 MiB source |
| DOCX/PPTX/XLSX and open document containers | >= 1.5x documents/s, size-stratified | <= 128 MiB at <= 32 MiB expanded source |
| Native PDF | >= 1.5x pages/s | <= 128 MiB at <= 100 fixture pages |
| Images/scanned PDF | report pages/s separately; no native-text speed proxy | owner sets OCR budget after engine evaluation |
| Email/archives/other families | >= 1.5x members/s where a Python baseline exists | <= 128 MiB with bounded expansion |

Report one/two/four-core scaling up to configured cap (target >= 1.5x at two
cores on CPU-bound eligible arms), cold/warm time, MiB/s or pages/cells/members/s,
peak RSS, language, failure type and corpus checksum. Unsupported baseline arms
are reported unavailable, not zero-speed wins. Fidelity, table association,
reading order, exact provenance and native-control non-regression are separate
hard gates: speed never compensates for lost evidence. Owner approval of the
ADR-0025 metric definitions/margins and held-out corpus precedes promotion.

## Proposed contract changes — OWNER APPROVAL REQUIRED

This is a proposal only; no AGENTS.md, CLAUDE.md or harness file is edited.
Proposed addition to root AGENTS.md section 3 and backend stack description:

> Heavy ingestion computation lives in a pure Rust core (`rust/crates/lumen-docintel`),
> exposed through a PyO3/maturin native extension. Python remains the only backend
> and owns API, auth, tenancy/permissions, orchestration, network/storage and audit.

Proposed module-ownership rows for root section 6 (and ADR-0004's living table):

| Concern | Single owning module |
|---|---|
| Ingestion computation and canonical representation | `rust/crates/lumen-docintel/` (pure Rust) |
| Python/native ingestion boundary | `backend/app/ingestion/native.py` with `rust/crates/lumen-docintel-py/` (conversion, GIL, errors) |

Only the facade imports `lumen_docintel`; existing Python parsers remain under
`backend/app/ingestion/` until approved per-format removal. These internal modules
do not acquire authority over any external system. Owner approval is required
before editing the actual contracts; accepting this draft alone does not edit them.

## Consequences

The computation boundary can scale while preserving existing trust chokepoints
and fallback behavior. Native wheels add platform builds, dependency review and
allocation/cancellation instrumentation. The model must preserve more than text
parity. Baseline measurements, production budgets, OCR/parser selection,
retention/backfill policy and contract-file edits remain owner decisions.
Implementation stays draft and a human merges after measured evaluation.
