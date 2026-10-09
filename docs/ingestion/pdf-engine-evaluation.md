# PDF extraction candidate — issues #671 and #672

ADR-0026 candidate decision, 2026-10-08. Production selection remains subject to
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
| pdfium-render + PDFium | wrapper MIT/Apache-2.0; engine BSD-3-Clause, bundled components separately reviewed | Native glyph boxes and fonts; still needs order/table reconstruction | Native engine; no measured local comparison; engine calls cannot be interrupted cooperatively | Requires process isolation for hard RSS/deadline and serialized engine calls. Docker/CI/Windows need separate pinned official platform binaries, checksums and notices. Not adopted; no binary downloaded. |
| lopdf + reconstruction | MIT | PDF operators, font maps and widths available; own glyph/layout interpretation required | Pure Rust; full object loading/decompression has a cost; not measured here | Parser/decompression is outside our Context accounting; requires instrumented loader or isolation. No native binary. |
| pdf-extract | MIT | OutputDev callbacks expose text transforms; plain text alone loses layout | Pure Rust interpreter atop lopdf; not measured here | Inherits loader constraints and font dependencies; no native binary. |
| hayro | MIT/Apache-2.0 | Renderer/interpreter; extraction needs a custom device; selection/search are outside stated scope | Pure Rust rendering work unnecessary for text-only ingestion; not measured here | Broader image/font graph and uninstrumented parsing; no native binary. |
| Bounded in-core PDF interpreter (this draft) | repository MIT/Apache-2.0; flate2 MIT/Apache-2.0 | Source text matrices and supplied widths; heuristic boxes explicitly labelled; column/line reconstruction | Token, expansion and output allocations charged before growth; actual fixture measurements recorded separately | Strict supported subset, typed unsupported for unimplemented constructs; no vendor loader, native binary or OS-specific packaging. |

GPL/AGPL/LGPL engines are excluded. The draft selects the bounded in-core
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

[~] 2026-10-08: implementation and fixture measurements pending this draft.
Residual risk: supported-subset coverage and heuristics need the held-out corpus.
Merge gate: hold until measured against the baseline evaluation; a human merges.
