# QA777 synthetic fixtures

These files contain only synthetic QA canaries. `manifest.json` records the exact
uploaded bytes, including the deliberately malformed 77-byte PDF. Original
execution used the committed QA-FILL definitions; later retention of a fixture
does not change the application or definition revision tested.

`qa777-docx-output-bound.docx` is a valid 38,626-byte DOCX with 2,000,020 extracted
characters. It exceeds the configured 2,000,000-character extraction output bound
without a large upload. The real worker rejected it before embedding, with zero
chunks and the safe message `DOCX extraction output limit exceeded`; direct text
access returned 409. This is a bounded extraction test, not an OOM, archive-bomb,
CPU-isolation, or general parser-containment certification.

The two injection TXT files contain harmless instruction-takeover canaries. They
were tested as owner and an explicitly granted member, while a distinct private
document stayed unshared. Keep the bytes unchanged when reproducing a finding.

The disposable TXT was previewed and downloaded, then deleted through Chrome
under the user's explicit cleanup approval. The retained local bytes are the
reproduction fixture and backup; they do not imply the product document remains.

See [the execution report](../../../docs/qa/777-residual-gaps.md) and the separate
immutable records under `qa/runs/`. Raw screenshots and reports stay in ignored
`output/qa/777`; the sanitized evidence index records their hashes and retention.
