# Rust ingestion foundation

ADR-0027 owns the boundary and merge gates. No production format is cut over.

From the repo root on Windows (MSVC + Visual Studio Build Tools), macOS (Xcode
command-line tools) or Linux (C linker and Python development headers):

```sh
uv sync --project backend --extra dev --extra native
```

After editing Rust, rebuild the local wheel with:

```sh
uv sync --project backend --extra dev --extra native --reinstall-package lumen-docintel
```

Run backend commands with `--extra native --extra dev` to keep uv from removing
this optional package. Python-only `uv run --extra dev pytest -m "not live"`
continues to work without Rust; bridge-only tests skip when it is absent.
The import is lazy and localized in `app.ingestion.native`.

On the memory-constrained Windows host check Available MBytes before every build
or test. Require 2500 MiB; wait 60 seconds and retry up to 20 minutes otherwise.
Keep `CARGO_BUILD_JOBS=2` and the configured Cargo target outside synced folders.
Never run pytest and Cargo builds/tests together. Docker builds require 6000 MiB.

```sh
cd rust
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cargo deny --config deny.toml check
```

The extension uses PyO3's Python 3.12 stable ABI. Cargo tests omit the extension
module feature so executable tests link correctly; maturin enables it for wheels.
The handshake test proves that a Python thread can release a native wait while
it is still pending. A five-second native watchdog prevents deadlock on failures;
passing is determined by handshake state, not measured elapsed time.

Docker uses `docker build -f backend/Dockerfile .` from the repo root. Compose
services use that same context and image; source mounts remain on `/app`.
Only the wheel crosses from the builder; runtime needs no Rust compiler.
CI builds Windows/macOS/Linux wheels and exercises bridge tests offline.

Dependency licenses: PyO3 MIT OR Apache-2.0; maturin MIT OR Apache-2.0 (build).
The lockfile and cargo-deny policy cover the transitive Rust graph. No parser,
network/storage client or OCR engine is introduced in this foundation.
The full dependency gate checks licenses, advisories, bans and sources. Local
workspace path dependencies also carry an exact version to satisfy the wildcard
ban and detect an unintended core/bridge version mismatch.

References: [PyO3](https://pyo3.rs/), [maturin configuration](https://www.maturin.rs/config),
[uv optional path dependencies](https://docs.astral.sh/uv/concepts/projects/dependencies/).

The PyO3 build dependency `target-lexicon` uses Apache-2.0 WITH LLVM-exception.
The exception permits linking compiled portions without additional attribution
conditions; it adds permission, not copyleft. It is explicitly allowlisted.
Reference: https://spdx.org/licenses/LLVM-exception.html.

Canonical schema v1: `docs/ingestion/canonical-schema-v1.md`. Native rendering
releases the GIL and returns immutable domain views through the Python facade.
Model parity does not imply native format-parser parity. New dependencies:
serde/serde_json (MIT OR Apache-2.0), proptest (MIT OR Apache-2.0, tests only).

Detection v1 is inert until a format parser lands and is enabled. It never
changes the upload allowlist. See `docs/ingestion/detection-v1.md`. Dependencies:
zip/infer/cfb (MIT), chardetng (Apache-2.0 OR MIT), encoding_rs
((Apache-2.0 OR MIT) AND BSD-3-Clause). All operations use in-memory bytes.

Runtime v1 (`docs/ingestion/runtime-v1.md`) uses rayon 1.12.0 (MIT OR Apache-2.0).
Instantiate one executor per worker child after fork. The optional
`docker-compose.ingestion-native.yml` bounds worker processes; it enables no
parser. Memory diagnostics count tracked Rust allocations, not process RSS.
Streaming windows retain the document deadline and output/work counters.

PDF candidate (#671): `formats::pdf::extract(bytes, Budget)` is a strict bounded
interpreter with positioned glyphs, heuristic reading order/headings, source
page boxes, metadata/bookmarks and typed per-page OCR outcomes. It adds flate2
1.1.10 and sha2 0.10.9 (both MIT OR Apache-2.0), without native binaries.
The backend facade's `parse_pdf_candidate(mode="python"|"shadow"|"native")`
is an independent PDF opt-in seam; normal `parsers.py` stays live. Shadow never
replaces Python text. Stage wiring and production cutover remain #669/#687.
See [engine evaluation](../docs/ingestion/pdf-engine-evaluation.md) and generated
[layout benchmark](../docs/ingestion/pdf-layout-benchmark.json). Supported-subset
coverage must be evaluated before promotion; broader coverage is tracked in #722.

PDF tables (#672) add painted-rule and conservative alignment detection, sparse
and spanning origin cells, header-labelled rendering and geometry/header-gated
continuation joining. Exact native page maps address disjoint table-text ranges;
cell boxes retain their original page. No additional crate or binary is added.
See [table acceptance](../docs/ingestion/pdf-tables-v1.md) and
[table benchmark](../docs/ingestion/pdf-tables-benchmark.json).

PDFium candidate (#722): Windows developer binary setup is one command from
the repo root (Python 3.12+): `python scripts/setup-pdfium.py`. It verifies the
pinned chromium/7881 archive and installs the library plus complete notices into
the ignored wheel-asset directory. Then use the existing `uv sync` command above
to build/rebuild the extension. CI and Docker run the same downloader before
building wheels; there are no runtime downloads or system-library lookups.

`parse_pdf_candidate` uses PDFium only in its explicit shadow/native modes.
`PdfiumExecutor` supervises recycled process slots, separate from rayon; construct
after fork. `configured_pdfium_executor(Settings)` exposes `native_pdf_workers`
(0 = auto), `native_pdf_worker_memory_bytes` (256 MiB OS cap) and
`native_pdf_pool_memory_bytes` (512 MiB pool cap). Celery concurrency multiplies
the total cap. These are engineering defaults, not approved production budgets.
The original `NativeExecutor.extract_pdf` remains the bounded evaluation baseline;
there is no automatic engine fallback after a failed extraction.

Opt-in local aggregate probe (from `backend/`, path supplied by the evaluator):
`uv run --extra dev --extra native python -m tests.eval.docintel.pdf_corpus_probe
--corpus-directory <directory> --workers 2`. It streams inputs, ignores suffixes,
skips PDFs over 100 MiB and emits aggregates only. Do not use the older external
manifest benchmark for private documents: that mode records input identities.
See [acceptance contract](../docs/ingestion/pdfium-engine-v1.md),
[binary pins](pdfium-binaries.json) and [native notices](NOTICE.pdfium).

macOS wheels verify the pinned engine and notices, but untrusted PDFium extraction
fails closed before input: an enforceable OS memory cap is not available through
RLIMIT_AS on tested macOS runners. Follow-up [#738](https://github.com/k-sandhu/lumen-copilot/issues/738)
tracks native macOS isolation; Python remains live.
