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

100 pinned entries (~150 MB): 98 ingestable real-world files spanning **every
upload-allowlisted format** (PDF, DOCX, PPTX, XLSX, TXT, MD) and **two
deliberate negatives** in formats outside the allowlist (CSV, HTML) that must
be rejected fail-closed. Sizes run from ~0.5 KB to ~32 MB (all under
`MAX_UPLOAD_BYTES` = 50 MiB); domains span literature, web standards, ML
research, tax, digital-identity security, climate science, official statistics,
and clinical research; languages are English plus one German text.

The **benchmark slice** the question bank is authored against is the original
~40 entries; the remaining 60 are the tax-research packs' documents (see
[Tax-research packs](#tax-research-packs-515) below), which no question cites.
Nothing downloads unless you ask for it: `download` fetches the whole corpus,
but `load_pack --pack <id>` fetches only that pack's files. One entry
(`nidcr-site-activation-checklist`, a checklist DOCX whose content lives in
Word tables) is pinned `text_quality="poor"`: the paragraphs-only DOCX parser
extracts almost nothing from it, a real product gap this dataset surfaced —
filed as [#423](https://github.com/k-sandhu/lumen-copilot/issues/423). No
questions reference it, and a test asserts the limitation stays pinned until
the parser learns tables.

Sources are trusted public hosts only (Project Gutenberg, RFC Editor, arXiv,
IRS, NIST, IPCC, US Census Bureau, NIH NIDCR, UN Statistics Division, the NYS
Department of Taxation and Finance, the Canada Revenue Agency, Justice Laws
Canada, the Ontario Central Forms Repository, and tag-pinned GitHub raw files),
fetched with an honest project User-Agent —
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

## Industry packs (#443)

Five curated packs give specialized teams the documents their industry
actually loads into AI knowledge assistants. The industry/document mapping is
evidence-based: financial services leads adoption (~84%, ~47% production
agents) and loads filings, shareholder letters and regulation; healthcare is
the fastest-accelerating vertical (labels, guidance, protocols); legal teams
load statutes and privacy regulation (contract/regulatory review is the
highest-ROI RAG use case in practitioner surveys); engineering orgs load
specs/vendor docs/changelogs; and vertical AI for government/healthcare/legal
roughly tripled to a ~$3.5B category. Sources: [Deloitte State of AI in the
Enterprise](https://www.deloitte.com/us/en/what-we-do/capabilities/applied-artificial-intelligence/content/state-of-ai-in-the-enterprise.html),
[NVIDIA State of AI 2026](https://blogs.nvidia.com/blog/state-of-ai-report-2026/),
adoption-statistics roundups ([Azumo](https://azumo.com/artificial-intelligence/ai-insights/enterprise-ai-adoption-statistics),
[Presenc](https://presenc.ai/research/ai-adoption-by-industry)), and RAG
use-case surveys ([Uptech](https://www.uptech.team/blog/rag-use-cases),
[Dust](https://dust.tt/blog/rag-use-cases-real-business-problems),
[Hebbia](https://www.hebbia.com/resources/ai-powered-enterprise-search)).

| pack id | files | flavour |
|---|---|---|
| `healthcare-life-sciences` | 4 | FDA drug labels ×2, NIDCR clinical monitoring guidance + template |
| `financial-services` | 5 | Berkshire letters ×2, Basel III finalisation, IRS 1040 instructions (2024 pinned + **current rolling**) |
| `legal-compliance` | 5 | GDPR, Copyright Circular 1, US Constitution, Federalist Papers, NIST SP 800-63B |
| `software-cloud-engineering` | 8 | HTTP RFCs, ECMA-404, AWS Well-Architected, Intel SDM, K8s changelog, pandoc, FastAPI |
| `government-data-climate` | 7 | Census tables + Statistical Abstract, IPCC AR6 ×2, UNSD decks |
| `tax-research-new-york` | 36 | see [Tax-research packs](#tax-research-packs-515) |
| `tax-research-ontario` | 26 | see [Tax-research packs](#tax-research-packs-515) |

```powershell
# from backend/ — browse the catalog, then load one into your profile:
uv run --extra dev python -m tests.eval.benchmark.load_pack --list-packs
uv run --extra dev python -m tests.eval.benchmark.load_pack `
  --api http://localhost:47181 --email you@acme.test --password ... `
  --pack financial-services
```

`--pack` composes with `--formats`/`--count` (curated pack order preserved;
first-N, no round-robin). Packs never include the negative-format or
poor-extraction files — a curated pack is all signal.

**Rolling entries (refresh on demand).** Entries marked `rolling` point at a
source's *current* alias (the IRS current-tax-year 1040 instructions, the NYS
sales-tax and withholding publications, the consolidated Canadian statutes).
Their checksum pin is a last-seen record, not a gate; `--refresh` re-downloads
them and **replaces** them in your profile (delete + re-upload). Benchmark
questions may never cite rolling files — grounding stays on immutable pinned
files, enforced by validation.

## Tax-research packs (#515)

Two packs give a tax team every document a company or an individual meets when
filing in one jurisdiction — **`tax-research-new-york`** (36 files, US federal +
New York State) and **`tax-research-ontario`** (26 files, Canadian federal +
Ontario).

**Why tax.** Tax research is the profession's fastest-moving AI use case and the
one least tolerant of an ungrounded answer: weekly use of AI for tax research
rose from ~33% to ~60% of practitioners in a single year, a third of tax firms
already use generative AI (14% agentic, 63% considering or planning it), and tax
preparation/research shows the highest generative-AI uptake in the profession.
Sources: [Blue J / CPA.com AI tax survey](https://www.cpa.com/news/blue-j-and-cpacom-survey-finds-ai-adoption-among-tax-firms-has-nearly-doubled-one-year)
([coverage](https://www.cpapracticeadvisor.com/2026/06/02/blue-j-and-cpa-com-survey-finds-ai-adoption-among-tax-firms-has-nearly-doubled-in-one-year/184376/)),
[Thomson Reuters Institute — AI in Professional Services 2026](https://www.thomsonreuters.com/en-us/posts/technology/ai-in-professional-services-report-2026/),
[Thomson Reuters — impact of AI on tax and accounting](https://tax.thomsonreuters.com/blog/the-impact-of-ai-on-the-tax-and-accounting-profession/),
[CPA Trendlines Outlook 2026](https://cpatrendlines.com/2026/01/10/outlook-2026-agentic-ai-reaches-the-tipping-point-in-tax-and-accounting-firms/).

### Coverage is a validated property, not a claim

A pack in the `tax-research` family must map **every** topic below onto at least
one of its own files, and every file it carries must serve at least one topic.
`pack_issues()` fails the build otherwise, so "covers all aspects of tax" is
enforced rather than asserted — drop the last document behind a topic and the
tests go red.

| topic | what it covers | NY | ON |
|---|---|---|---|
| `personal_income` | Individual income tax return and its computation | 5 | 4 |
| `business_income` | Corporate / unincorporated business income tax | 4 | 4 |
| `pass_through` | Partnerships, S corporations, elective entity-level tax | 4 | 1 |
| `payroll_withholding` | Employer withholding, remittance, wage reporting | 5 | 4 |
| `consumption_tax` | Sales & use tax / GST-HST on supplies | 3 | 4 |
| `property_transfer` | Property tax and real-estate transfer tax | 2 | 3 |
| `credits_deductions` | Credits, deductions, depreciation, capital cost | 3 | 3 |
| `cross_border` | Non-resident, part-year, multi-jurisdiction allocation | 3 | 2 |
| `estates_trusts` | Estate tax, trusts, fiduciary returns | 2 | 1 |
| `filing_procedure` | Deadlines, instalments, elections, how to file | 5 | 3 |
| `disputes_penalties` | Audit, objection/appeal rights, penalties, interest | 2 | 1 |
| `primary_authority` | The statute, regulation or official ruling itself | 3 | 2 |
| `reference_data` | Rate schedules, threshold tables, statistics | 4 | 2 |

A file may serve several topics (New York's IT-112-R resident credit is both
`cross_border` and `credits_deductions`), which is why the columns sum to more
than the pack size.

### What's in them

**`tax-research-new-york`** — a New York filing question is never answerable
from one jurisdiction, so the pack is deliberately two-layered:

- *Federal (IRS, public domain):* Publication 17, the Form 1040 instructions
  (2024 pinned **and** current-year rolling), Forms 1120 / 1120-S / 1065 /
  1041 / Schedule C instructions, Publication 15 (Circular E) and the W-2/W-3
  instructions, Publication 946 (depreciation), 505 (estimated tax), 519
  (aliens), 556 (appeals) and 1 (taxpayer rights), **Internal Revenue Bulletin
  2025-01** and **Rev. Proc. 2024-40** as primary authority, plus the SOI
  New York individual-return **XLSX** as reference data.
- *New York State (NYS Dept. of Taxation and Finance):* IT-201 resident and
  IT-203 nonresident instructions, IT-112-R resident credit, IT-225
  modifications, IT-2105 estimated tax, CT-3 franchise tax, CT-3-S, IT-204
  partnership, **TSB-M-21(1)C,(1)I** (the Department's own PTET memorandum),
  Publication 750 sales-tax guide, Publication 718 rates by jurisdiction,
  ST-100 quarterly return, NYS-45 employer return, NYS-50-T-NYS withholding
  tables, MTA-305 (MCTMT), ET-706 estate tax, TP-584 transfer tax, and
  Publication 1093 (veterans' property-tax exemption).

**`tax-research-ontario`** — Ontario's personal and corporate income tax is
computed on the federal base and administered by the CRA, so the CRA guides
*are* the Ontario authority:

- *Federal + Ontario (CRA):* 5000-G federal guide, **5006-PC Ontario
  Information Guide** and **5006-C (ON428) Ontario Tax**, T4012 T2 corporation
  guide with **Schedule 500 (Ontario tax calculation)** and **Schedule 510
  (Ontario corporate minimum tax)**, T4002 self-employed, T4068 partnership
  (T5013), T4001 payroll deductions, T4130 taxable benefits, RC4110
  employee-or-self-employed, RC4022 GST/HST registrants, RC4058 quick method,
  RC4028 new-housing rebate, T4037 capital gains, T4036 rental income, T4044
  employment expenses, T4013 T3 trust guide, T4058 non-residents, T4144
  section 216, P105 students, P148 objection and appeal rights.
- *Ontario-administered:* the **Employer Health Tax** return guide and the
  **Land Transfer Tax Affidavit** from the Ontario Central Forms Repository —
  the two taxes Ontario collects itself.
- *Primary authority:* the consolidated **Income Tax Act** and **Excise Tax
  Act** from Justice Laws Canada (rolling — a consolidation is re-published on
  every amendment).

### Pin stability

Tax documents are republished every filing season, so each entry uses the most
immutable URL its source offers and is marked `rolling` only when none exists:

| source | immutable form used | rolling instead |
|---|---|---|
| IRS | prior-year archive (`/pub/irs-prior/p17--2024.pdf`); bulletins and rev. procs. are dated by nature | Pub 556 and Pub 1 — revision-dated, no year-stamped archive |
| NYS | per-year archive for annual income/corporation forms (`/pdf/2024/inc/it201i_2024.pdf`); TSB-M memoranda are dated | sales-tax, withholding, estate and property documents, which exist only at their current path |
| CRA | the tax year is in the filename (`t4012-25e.pdf`), so each year is its own URL | — |
| Justice Laws / Ontario CFR | — | consolidated statutes and CFR forms are served in place |

### Loading one

```powershell
# from backend/ — the whole New York pack into your profile:
uv run --extra dev python -m tests.eval.benchmark.load_pack `
  --api http://localhost:47181 --email you@acme.test --password ... `
  --pack tax-research-new-york --collection "NY tax research"

# just one aspect of filing — e.g. the Ontario payroll documents:
uv run --extra dev python -m tests.eval.benchmark.load_pack `
  --pack tax-research-ontario --tax-topic payroll_withholding --dry-run

# pull the current statutes / current-season forms again:
uv run --extra dev python -m tests.eval.benchmark.load_pack ... `
  --pack tax-research-ontario --refresh
```

`--tax-topic` composes with `--formats` and `--count` and preserves curated pack
order. Filters that leave nothing to load are an **error**, not an empty run.

### Deliberate gaps

Recorded rather than papered over:

- **New York City** business taxes (NYC-2 Business Corporation Tax, NYC-202
  UBT) are absent: `nyc.gov` answers **403** to our honest User-Agent, and the
  corpus policy is to exclude bot-walled hosts rather than evade them. NYC
  *personal* income tax is still covered — it is computed on the IT-201.
- **Ontario's own consolidated statutes** (Taxation Act 2007, Employer Health
  Tax Act) are HTML-only on e-Laws, which is outside the upload allowlist;
  `files.ontario.ca` is bot-walled. Federal statutes carry `primary_authority`
  for the Ontario pack, which is legally where Ontario income tax is computed.
- **No benchmark questions** are authored against these 60 files yet — the
  question bank still measures the original ~40-entry slice. Grounded tax
  questions are follow-up work; nothing here changes the reference run.

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
