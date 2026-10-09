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
