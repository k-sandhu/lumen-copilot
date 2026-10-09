# PDF extraction candidates — issues #671, #672 and #722

ADR-0027 candidate decision, 2026-10-08. Production selection remains subject to
the owner's held-out baseline evaluation. Python pypdf stays authoritative by default.

## Engine comparison

These are design comparisons, not measured performance claims for unbuilt engines.
Upstream sources: [pdfium-render](https://github.com/ajrcarey/pdfium-render),
[PDFium](https://pdfium.googlesource.com/pdfium/),
[lopdf](https://github.com/J-F-Liu/lopdf/blob/main/Cargo.toml),
[pdf-extract](https://github.com/jrmuizel/pdf-extract), and
[hayro](https://github.com/LaurenzV/hayro).

| Candidate | Licence | Position fidelity / reading order | Speed and memory | Malformed input / packaging |
|---|---|---|---|---|
| pdfium-render + PDFium (#722) | wrapper MIT/Apache-2.0; engine BSD-3-Clause, bundled permissive component notices retained | Native character boxes and fonts feed the existing order/table reconstruction | OS-supervised recycled processes; aggregate corpus measurement gates selection | Pinned non-V8 chromium/7881 release archives, five platform SHA-256 pins, verified download and wheel notices. Python remains live; acceptance in [pdfium-engine-v1.md](pdfium-engine-v1.md). |
| lopdf + reconstruction | MIT | PDF operators, font maps and widths available; own glyph/layout interpretation required | Pure Rust; full object loading/decompression has a cost; not measured here | Parser/decompression is outside our Context accounting; requires instrumented loader or isolation. No native binary. |
| pdf-extract | MIT | OutputDev callbacks expose text transforms; plain text alone loses layout | Pure Rust interpreter atop lopdf; not measured here | Inherits loader constraints and font dependencies; no native binary. |
| hayro | MIT/Apache-2.0 | Renderer/interpreter; extraction needs a custom device; selection/search are outside stated scope | Pure Rust rendering work unnecessary for text-only ingestion; not measured here | Broader image/font graph and uninstrumented parsing; no native binary. |
| Bounded in-core PDF interpreter (this draft) | repository MIT/Apache-2.0; flate2 MIT/Apache-2.0 | Source text matrices and supplied widths; heuristic boxes explicitly labelled; column/line reconstruction | Token, expansion and output allocations charged before growth; actual fixture measurements recorded separately | Strict supported subset, typed unsupported for unimplemented constructs; no vendor loader, native binary or OS-specific packaging. |

GPL/AGPL/LGPL engines are excluded. The #671/#672 drafts select the bounded in-core
interpreter as a measurable, fail-closed candidate. It does **not** claim equivalent
coverage to a complete PDF renderer. A broader engine must retain the budget
boundary before promotion; unsupported cases count against corpus coverage.

## Acceptance and safety contract

- Pure `formats::pdf::extract(bytes, limits)` returns canonical blocks or a safe
  typed error. Shared Context and the #666 Runtime parallelize independent pages.
- Limits cover input, token/object depth, reference cycles, expanded content,
  glyph/layout work, output, memory reservations and elapsed time. Flate decoding
  uses fixed output windows with checkpoints; images are never decompressed.
- Encrypted input is rejected without password attempts. Corrupt references,
  stream lengths and page trees fail closed. Unsupported encodings/operators
  never quietly drop text. Object streams, incremental updates and complex
  shaping are not accepted by the initial bounded subset.
- Positions use PDF points and bottom-left origin in the unrotated source page.
  Display rotation is retained in diagnostics; boxes never pretend to be exact
  ink extents when only advance widths are available.
- Reconstructed lines/blocks retain page/box provenance and deterministic
  Unicode code-point spans. SourcePart maps include pages without text.
- Reading order: spanning titles precede vertical column bands, left to right
  within each band; sidebars are separate narrow columns; small bottom text is
  a heuristic footnote. Font size/weight heading roles are heuristic.
- Repeated margin text is retained as Furniture with exact evidence spans;
  normalization #668 may exclude its IDs from retrieval. De-hyphenation is
  derived metadata for soft hyphens only; hard hyphens are ambiguous and retained
  so identifiers and historical evidence cannot be changed.
- Per-page typed `needs_ocr` diagnostics distinguish unavailable text from
  successfully extracted pages; document outcome is `needs_ocr` / `partial`.
  No network, OCR, persistence or generation activation is added.
- Metadata/bookmarks are inert supplied text. No scripts, links or actions execute.
- Backend integration is additive through native.py, with independent PDF
  python/shadow/native routing; shadow failures preserve Python output.

## Verification record

Generated Windows cold measurements (21 document-arm rows) are retained in
[pdf-layout-benchmark.json](pdf-layout-benchmark.json). Every unsuccessful
document stays in the denominator. The provenance baseline is a PDF-only
projection equivalent to #625; #630/#638 persistence and diagnostics are read
for compatibility but are not merged or presented as a full integrated baseline.

| Generated layout fixture | Current pypdf | #625-equivalent pypdf | Rust candidate |
|---|---:|---:|---:|
| Fact coverage | 100% | 100% | 100% |
| Reading-order pairs | 66.7% | 66.7% | 100% |
| Exact code-point spans | 100% | 100% | 100% |
| Page provenance | unavailable | 100% | 100% |
| Cold extraction incl. parser import | 303.9 ms | 416.8 ms | 26.6 ms |
| Process peak RSS | 63.05 MiB | 62.68 MiB | 23.31 MiB |
| Incremental peak RSS | 40.79 MiB | 40.42 MiB | 1.24 MiB |

These tiny cold fixtures do not establish steady-state throughput or answer
accuracy. The original #670 digital fixture includes a blank page: Rust retains
all digital facts/page maps and reports partial/needs-OCR conservatively rather
than deciding that blank output is an intentional blank page. Scanned and mixed
pages remain incomplete. Rotation and repeated furniture fixtures retain all
facts and exact spans. Original hard-hyphen evidence remains immutable; only
soft-hyphen de-hyphenation is provided as a derived normalization hook.

Verified offline: 28 Rust workspace tests (12 PDF tests including property runs),
29 targeted Python parser/bridge/fidelity tests with a dedicated temporary root,
wheel build/import, strict mypy facade and Ruff. Tests first reproduced missing
PDF module/routing; negative tests cover malicious expansion, deep objects,
encrypted/corrupt input, cancellation and healthy reuse. No containers or live
datastores were exercised.

[~] 2026-10-08: full backend suite deferred under the 4000 MiB threshold
(3336 MiB available during final workspace testing); rely on CI. Residual risk:
unrelated backend regressions are not ruled out by targeted checks.

[~] 2026-10-08: local cargo-deny executable unavailable; pinned Cargo.lock and
the inherited Linux CI license/advisory gate cover new dependencies. Residual
risk: local transitive advisory scan is not verified.

[~] 2026-10-08: held-out corpus, warm throughput/scaling, Linux/macOS wheel
execution and full #625/#630/#638 integration remain unverified. Residual risk:
format coverage, script/font order and heuristic boxes may fail enterprise PDFs.
Broader engine coverage/containment is tracked separately in #722. Owner chooses
production budgets, evaluation thresholds and broader-engine strategy.
Merge gate: hold until measured against the baseline evaluation; a human merges.
