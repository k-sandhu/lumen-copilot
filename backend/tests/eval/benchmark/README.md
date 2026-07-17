# RAG benchmark dataset — real-world corpus + grounded question bank

A reproducible benchmark for measuring Lumen Copilot's RAG quality over **real
files**, extending the tiny in-code golden set (`tests/eval/golden.py`, #29)
with the scale, formats, and messiness the synthetic set cannot represent.
Tracked by [#420](https://github.com/k-sandhu/lumen-copilot/issues/420) and
extended by [#430](https://github.com/k-sandhu/lumen-copilot/issues/430)
(evidence-based question taxonomy, industry corpus slice, CSV bank, live
accuracy runner).

## What's here

| Piece | File | Committed? |
|---|---|---|
| Corpus manifest (URLs, licenses, expectations) | `manifest.py` | yes |
| Checksum pins (sha256 + size per file) | `checksums.json` | yes (machine-written) |
| Question bank (Excel-friendly CSV) | `questions.csv` | yes |
| Download CLI | `download.py` | yes |
| Extraction CLI (real ingestion parsers) | `extract.py` | yes |
| Verification CLI | `verify.py` | yes |
| Live accuracy runner (HTTP, end-to-end) | `run_live.py` | yes |
| Isolated bench-stack config | `compose.bench.yml` + `bench.env` | yes |
| Executable guarantees | `../test_benchmark_dataset.py` | yes |
| The corpus bytes | `corpus/` | **no — git-ignored, downloaded on demand** |
| Run reports | `corpus/_results/` | no — git-ignored |

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

`questions.csv` — 101 questions in a wide, Excel-friendly CSV (UTF-8 with BOM;
open it directly in a spreadsheet): 41 single-hop, 5 multi-hop, 6 aggregation,
6 keyword, 5 distractor, 2 multilingual, 4 condition, 3 set, 3 comparison,
3 post-processing, 3 false-premise, 3 procedural, 3 navigation, and 14
unanswerable (~14% — inside the enforced 10–25% band).

**Schema** (one row per question): `qid, category, difficulty, answerable
(yes/no), language, question, gold_answer, answer_facts, source_files,
evidence1_file/locator/quote, evidence2_file/locator/quote, distractor_files,
absence_probes, notes`. List cells are `|`-separated; up to two evidence spans
per row; quotes keep their real newlines inside quoted cells.

### The evidence base behind the categories

The category set follows published evidence about what users actually ask
systems like this:

- **[CRAG](https://arxiv.org/abs/2406.04744)** (Meta, NeurIPS 2024) built its
  benchmark from real user queries and found eight recurring types: simple,
  simple-with-**condition**, **set**, **comparison**, aggregation, multi-hop,
  **post-processing**-heavy, and **false-premise** questions. All eight are
  represented here.
- **[Benchmarking Deep Search over Heterogeneous Enterprise Data](https://arxiv.org/abs/2506.23139)**
  (Salesforce, 2025): enterprise questions require multi-hop reasoning across
  heterogeneous business artifacts and must include **unanswerable** queries.
- Practitioner query-intent reports ([Moveworks](https://www.moveworks.com/us/en/resources/blog/what-is-enterprise-search),
  [Slack](https://slack.com/blog/productivity/enterprise-search),
  [Kore.ai](https://www.kore.ai/blog/enterprise-search-use-cases)): the dominant
  workplace intents are **how-do-I** (→ `procedural`), **policy lookup**
  (→ `single_hop`/`condition`), and **find-the-artifact** (→ `navigation`).
  People-search is out of scope for a document-only corpus.
- In-repo: `docs/product/user-stories.md` phrases a dozen stories as exactly
  these intents.

| Category | Probes | Scored on |
|---|---|---|
| `single_hop` | basic retrieval + extraction from one passage | recall, citation, answer facts |
| `multi_hop` | combining evidence across ≥ 2 documents | per-document recall |
| `aggregation` | reading tables (XLSX rows, PDF tax/stat tables) | answer facts |
| `keyword` | rare-token lookup (CVE ids, option names, codes) — lexical retrieval must win | recall |
| `distractor` | a named near-miss document exists; the right source must be discriminated | citation correctness |
| `multilingual` | evidence lives in the German document | recall across languages |
| `condition` | the answer depends on a stated condition (one table row/branch) | answer facts |
| `set` | list answers (pillars, categories, token sets) | all facts present |
| `comparison` | two named things compared on one attribute | both operands cited |
| `post_processing` | computing over retrieved values (differences, growth) | operand facts |
| `false_premise` | the question embeds a wrong assumption; sources must win over the premise | correction facts |
| `procedural` | "how do I…" workplace intent | answer facts |
| `navigation` | "which document covers…" artifact-finding | self-identifying evidence |
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

## Load the data pack into your own profile (#441)

`load_pack.py` seeds the corpus into **any signed-in user's account** on a
running stack — no eval, no bench user, just real documents in your profile.
Pick the formats and the count; the same flags always select the same files:

```powershell
# from backend/ — preview the deterministic selection (nothing is touched):
uv run --extra dev python -m tests.eval.benchmark.load_pack --formats pdf,xlsx --count 5 --dry-run

# load into YOUR profile on the primary dev stack:
uv run --extra dev python -m tests.eval.benchmark.load_pack `
  --api http://localhost:47181 --email you@acme.test --password ... `
  --formats pdf,docx,xlsx --count 10
```

**Determinism rule:** ingestable entries only (the deliberate negative-format
files are never offered), round-robin across your formats **in the order you
gave them** (default `txt, md, pdf, docx, pptx, xlsx`), each format's files in
manifest order. Missing files download first (checksum-verified); uploads are
idempotent by filename, so re-running is a safe no-op. Credentials may also
come from `LUMEN_PACK_EMAIL` / `LUMEN_PACK_PASSWORD`. The selection rule is
unit-tested (`tests/eval/test_benchmark_pack.py`), and the shared HTTP client
(`client.py`) survives the 15-minute access-token expiry mid-ingestion.

## Live accuracy run (#430)

`run_live.py` drives the REAL stack over HTTP end-to-end — login, uploads
(including the two files that must be rejected 415), Celery ingestion, then
per-question `/search` retrieval scoring and chat-API answer scoring (fact
recall, gold-file citations, refusal behaviour on unanswerables). Reports land
under `corpus/_results/<run-id>/` (per-question CSV + summary markdown/JSON).

Run it against an **isolated** compose project so the primary dev stack keeps
its data and models (`compose.bench.yml` + `bench.env`, 472xx ports, project
`lumen-bench` — bring-up commands in the overlay's header). The bench overlay
pins the #430 models: generation `tencent/hy3:free`, embeddings
`nvidia/llama-nemotron-embed-vl-1b-v2:free` (2048-dim → the overlay's header
documents the one-time bench-local `vector(2048)` column change; pgvector's
HNSW index caps at 2000 dims and is dropped there — retrieval reads OpenSearch,
ADR-0010, so only transitional storage is affected).

Free-tier `:free` models rate-limit hard: the overlay runs ingestion at
concurrency 2 with large embed batches and a long retry ladder. Expect the
full corpus to take tens of minutes to embed and the 101-question chat pass
about an hour.

```powershell
# after bring-up + seed (see compose.bench.yml header), from backend/:
uv run --extra dev python -m tests.eval.benchmark.run_live `
  --api http://localhost:47281 --email bench@lumen.test --password lumen-bench-local
```

### Reference run (2026-07-17, tencent/hy3:free + nemotron-embed-vl-1b-v2:free)

Point-in-time numbers from the first full run (101 questions, 0 errors; all 30
files ingested, both negatives rejected 415) — not a CI gate, kept here as the
baseline the dataset was proven against:

| layer | metric | value |
|---|---|---|
| retrieval | hit@5 / hit@10 | **1.00 / 1.00** (every category, incl. German + tables) |
| retrieval | MRR@10 | 0.95 |
| answers | fact recall | 0.73 (procedural/navigation 1.0 · set 0.92 · … · comparison 0.50 · false-premise 0.25) |
| answers | cited the gold file | 0.90 |
| answers | wrong refusals (answerable) | 0.23 — see below |
| unanswerable | refused in text | 13/14 (hallucination rate 0.07) |
| unanswerable | refused with **zero citations** (AC-3) | **0/14 — every refusal carried citations (bug filed)** |

What the slicing localized: retrieval (nemotron embeddings + BM25 hybrid) is
not the bottleneck on this corpus — generation behaviour is. The wrong-refusal
mass splits between genuine misses (mostly literature questions where the
model's own tool query missed, despite `/search` ranking the gold file #1) and
hedged answers that contain the right facts but lead with "couldn't find the
exact line…", which the substring refusal markers count as refusals. The
false-premise category is the weakest: the model refuses rather than corrects.
And the unanswerable slice exposed a real product gap — the runtime attaches
citations to refusal answers, which the eval contract (AC-3) forbids.

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
