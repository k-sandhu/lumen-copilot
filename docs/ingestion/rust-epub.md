# EPUB candidate (#681)

The candidate validates all ZIP names, CRCs, entry counts, expansion/ratio limits
before reading container.xml, OPF manifest/spine, EPUB 2 NCX or EPUB 3 navigation.
Chapters follow spine order. TOC titles are source metadata; injected TOC headings
have heuristic roles and separate TOC provenance. Missing titles remain unknown.
Every block carries chapter ordinal/member path. Supplied footnotes are retained.
One shared context bounds archive, XML and HTML computation; no chapter resets it.

DRM/encryption descriptors are typed unsupported in this conservative profile;
ZIP-level encryption and malformed packages produce typed package failures.
Traversal entries and references escaping the package root are rejected. No media
is embedded or fetched. NATIVE_EPUB_ENABLED and NATIVE_EPUB_SHADOW default false;
only enabled cutover plus a capable wheel admits application/epub+zip.

Acceptance: EPUB 2/3 spine and TOC order/titles, exact chapter provenance/Unicode
slices, metadata and footnotes, traversal/encryption/malformed/budget negatives.

Merge gate: hold until measured against the baseline evaluation; a human merges.

## Dependencies and verification

New direct crate: quick-xml 0.42.0 (MIT). ZIP handling reuses the office package
helper and existing zip 8.6.0 (MIT); HTML dependencies inherit #679. All four
cargo-deny checks passed offline.

Generated corpus: EPUB facts, associations, reading order and exact offsets are
100%. The Python path does not support EPUB. No performance qualification is
claimed from one tiny generated document.

[x] 2026-10-09: cargo test --manifest-path rust/Cargo.toml --test epub (2 tests);
cargo fmt --all --check, cargo clippy --workspace --all-targets -- -D warnings,
and cargo deny --offline check passed. Native Windows wheel rebuilt/imported.
[x] 2026-10-09: targeted pytest test_rust_epub.py + test_rust_html.py (5 passed),
configured mypy (6 files), and offline candidate fidelity runner passed.
[~] 2026-10-09: full backend suite deferred to CI: available RAM 1950 MiB was
below the required 4000 MiB. Residual risk: broader integration regressions.
[s] 2026-10-09: live service tests excluded by task instruction; no containers
were changed. Residual risk: deployment behavior remains unverified.
[~] 2026-10-09: production promotion awaits budget-aware detection #728,
canonical stage integration #667–#669, held-out fidelity/performance evaluation
and human merge. Residual risk: caller budgets and production chunk fidelity.
