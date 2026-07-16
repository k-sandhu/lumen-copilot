# RAG benchmark dataset — real-world corpus + grounded question bank

A reproducible benchmark for measuring Lumen Copilot's RAG quality over **real
files**, extending the tiny in-code golden set (`tests/eval/golden.py`, #29)
with the scale, formats, and messiness the synthetic set cannot represent.
Tracked by [#420](https://github.com/k-sandhu/lumen-copilot/issues/420).

## What's here

| Piece | File | Committed? |
|---|---|---|
| Corpus manifest (URLs, licenses, expectations) | `manifest.py` | yes |
| Checksum pins (sha256 + size per file) | `checksums.json` | yes (machine-written) |
| Question bank | `questions.jsonl` | yes |
| Download CLI | `download.py` | yes |
| Extraction CLI (real ingestion parsers) | `extract.py` | yes |
| Verification CLI | `verify.py` | yes |
| Executable guarantees | `../test_benchmark_dataset.py` | yes |
| The corpus bytes | `corpus/` | **no — git-ignored, downloaded on demand** |

## Quick start

```powershell
# from backend/ — fetch + verify the corpus (idempotent, ~80 MB total)
uv run --extra dev python -m tests.eval.benchmark.download

# extract plain text with the real parsers (caches under corpus/_extracted/)
uv run --extra dev python -m tests.eval.benchmark.extract

# verify everything: pins, extraction, evidence grounding, absence probes
uv run --extra dev python -m tests.eval.benchmark.verify

# the same guarantees as tests (corpus-dependent ones skip if not downloaded)
uv run --extra dev pytest tests/eval/test_benchmark_dataset.py
```

`LUMEN_BENCHMARK_CORPUS_DIR` relocates the corpus outside the checkout if
desired. `download --smoke` fetches only the one-file-per-format smoke subset.

## The corpus

27 pinned entries (~80 MB): 25 ingestable real-world files spanning **every
upload-allowlisted format** (PDF, DOCX, PPTX, XLSX, TXT, MD) and **two
deliberate negatives** in formats outside the allowlist (CSV, HTML) that must
be rejected fail-closed. Sizes run from ~0.5 KB to ~32 MB (all under
`MAX_UPLOAD_BYTES` = 50 MiB); domains span literature, web standards, ML
research, tax, digital-identity security, climate science, official statistics,
and clinical research; languages are English plus one German text. One entry
(`nidcr-site-activation-checklist`, a checklist DOCX whose content lives in
Word tables) is pinned `text_quality="poor"`: the paragraphs-only DOCX parser
extracts almost nothing from it, a real product gap this dataset surfaced —
filed as [#423](https://github.com/k-sandhu/lumen-copilot/issues/423). No
questions reference it, and a test asserts the limitation stays pinned until
the parser learns tables.

Sources are trusted public hosts only (Project Gutenberg, RFC Editor, arXiv,
IRS, NIST, IPCC, US Census Bureau, NIH NIDCR, UN Statistics Division, and
tag-pinned GitHub raw files), fetched with an honest project User-Agent —
hosts that reject it (bot walls) are excluded by policy rather than evaded.
Each manifest entry records provenance and license; the corpus is downloaded
for local benchmarking, never redistributed via the repo.

**Reproducibility.** Every entry's sha256 + byte size is pinned in
`checksums.json`; the downloader streams to a temp file, checks magic bytes,
verifies the pin, and only then installs the file. A pin mismatch means the
upstream document genuinely changed — re-pin deliberately with
`download --pin` and treat the diff as a dataset change (questions referencing
the file must be re-verified; `verify` does exactly that).

**Size-limit negative.** There is intentionally no >50 MiB entry: the 413
oversize-rejection path is already unit-tested at the API layer, and forcing a
60 MB download to re-prove it buys nothing. To exercise it manually, upload
any file larger than `MAX_UPLOAD_BYTES`.

## The question bank

`questions.jsonl` — 71 questions, one JSON object per line (JSONL for
portability into external eval tooling): 35 single-hop, 5 multi-hop,
6 aggregation, 6 keyword, 5 distractor, 2 multilingual, and 12 unanswerable
(~17% — inside the enforced 10–25% band). Each category probes a different
failure mode:

| Category | Probes | Scored on |
|---|---|---|
| `single_hop` | basic retrieval + extraction from one passage | recall, citation, answer facts |
| `multi_hop` | combining evidence across ≥ 2 documents | per-document recall |
| `aggregation` | reading tables (XLSX rows, PDF tax/stat tables) | answer facts |
| `keyword` | rare-token lookup (CVE ids, option names, codes) — lexical retrieval must win | recall |
| `distractor` | a named near-miss document exists; the right source must be discriminated | citation correctness |
| `multilingual` | evidence lives in the German document | recall across languages |
| `unanswerable` | nothing in the corpus answers it | honest zero-citation refusal (AC-3) |

Every question carries: a concise `gold_answer`; `answer_facts` (tokens an
answer must contain — each fact provably occurs inside the question's own
evidence quotes); `evidence` spans with a human `locator` and a **verbatim
`quote` of the text the real parsers extract** (whitespace-normalized matching,
mirroring `tests.eval.metrics`); and for unanswerable questions,
`absence_probes` — strings machine-checked to occur **nowhere** in the
extracted corpus, so "unanswerable" is a verified property, not an opinion.

Quotes are authored ≤ ~200 normalized chars (hard cap 300) and aligned to
sentence boundaries so a production chunk (`INGESTION_CHUNK_SIZE=1200`,
overlap 200) can contain them — gold passages are retrievable and citable *by
construction*.

### Best practices this dataset encodes

1. **Ground truth is machine-verifiable** — every quote is checked against real
   parser output; every answer fact is checked against its quotes; every
   absence probe is checked against the whole corpus. `verify` / pytest fail
   on any drift.
2. **Negative controls** — unanswerable questions (10–25% of the bank, floor
   enforced) measure hallucination pressure; disallowed-format files prove
   fail-closed ingestion; a scanned PDF pins the OCR gap honestly.
3. **Distractor pairs by design** — same-author novels (Austen ×2), adjacent
   HTTP RFCs (9110/9112), paired ML papers, state vs county population tables:
   retrieval must pick the *right* confusable source, and `distractor`
   questions name their near-misses explicitly.
4. **Coverage floors, not vibes** — minimum question counts per category and
   per-format corpus coverage (incl. the smoke subset) are enforced by
   validation, so the bank keeps its shape as it evolves.
5. **Chunker-agnostic gold** — evidence is (document, quote), not chunk ids;
   scoring uses substring overlap after whitespace normalization, so chunking
   changes don't invalidate the dataset.
6. **Provenance + licensing recorded** per file; binaries out of git; honest
   User-Agent; versioned/immutable URLs wherever the source offers them.

## Measuring RAG accuracy with it

The single-evidence slice adapts directly into the existing eval harness
(`tests/eval/harness.py`) via `tests.eval.benchmark.bank`:

```python
from tests.eval.benchmark import golden_documents, golden_questions

docs = golden_documents(subset="smoke")   # or "full" (embeds a lot more text)
questions = golden_questions(subset="smoke")
# then exactly like tests/eval/test_eval_live.py:
#   seed_corpus(..., documents=docs, store=...)
#   scores = await run_eval(..., questions=questions)
```

- `subset="smoke"` — one real file per format (~1.5 MB total), cheap enough for
  a routine live run against the full stack.
- `subset="full"` — every good-extraction file (~75 MB of text→chunks); use for
  deliberate benchmark sessions, not CI.

Metrics come from the harness unchanged: **retrieval recall** (was the gold
passage retrieved), **citation correctness** (cited the right permitted
source), **groundedness** (no uncited claims), plus the refusal behaviour on
the unanswerable slice. Multi-evidence categories (`multi_hop`, some
`aggregation`) carry richer gold than `GoldenQuestion` can express; they are
consumed by `verify` today and await a fuller runner (follow-up work) —
slice-aware reporting (per category / format / size / language) can be built
on the `BenchmarkQuestion` fields as-is.

For **manual end-to-end testing**, upload files from `corpus/` through the UI
(they are real uploads exercising the real ingest path, including the two
that must be rejected) and spot-check questions from the bank in chat — every
question's `evidence.locator` tells a human where the answer lives.

## Maintenance

- **A pin broke** (`checksum mismatch`): the upstream file changed. Decide
  deliberately: re-pin (`download --pin`) and re-run `verify` so every quote is
  re-proven against the new bytes; or swap the entry for a stabler source.
- **Adding a file**: add the `CorpusFile` entry (license + provenance
  required), `download --pin`, extract, author questions against
  `corpus/_extracted/<file_id>.txt`, run `verify`.
- **Adding questions**: quotes must be copied verbatim from the extraction
  dumps (not from the original file) — that is what makes them provable.
  `verify` is the gate; the category floors keep the distribution honest.
