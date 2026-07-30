"""The benchmark corpus manifest — pinned real-world files, no committed binaries (#420).

Each :class:`CorpusFile` pins one real document from a **trusted public source**
(Project Gutenberg, the RFC Editor, arXiv, IRS/NIST/Census/NIH, the IPCC, the UN
Statistics Division, or a tag-pinned GitHub raw URL) with its provenance,
license, language, and expected pipeline behaviour. The bytes themselves live in
a git-ignored corpus dir; ``checksums.json`` (committed, machine-written by
``download --pin``) carries the sha256 + byte size of every entry so a re-download
is verified bit-for-bit.

Selection principles (see README.md for the full methodology):

* **Every upload-allowlisted format** (spec 0004 / #22): PDF, DOCX, PPTX, XLSX,
  ``text/plain``, ``text/markdown`` — questions must exercise each parser.
* **A size ladder** per the derived :func:`size_class` (tiny < 100 KB < small
  < 1 MB < medium < 10 MB ≤ large), up to well under ``MAX_UPLOAD_BYTES``.
* **Domain + language diversity** so retrieval has to discriminate: literature,
  web standards, ML research, tax, digital-identity security, climate science,
  official statistics, clinical research; English plus one German text.
* **Deliberate negatives**: two files in formats *outside* the allowlist
  (``expected_ingest="rejected_type"``) prove the fail-closed path, and one
  scanned image-only PDF (``text_quality="poor"``) pins the known OCR-less
  extraction limitation.
* **Stability**: URLs are versioned/immutable where the source offers it
  (tagged raws, arXiv versions, prior-year IRS archive, final reports). A pin
  that breaks means the upstream file genuinely changed — re-pin deliberately
  with ``download --pin`` and review the diff.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# MIME constants mirror app.ingestion.parsers (the upload allowlist) plus the
# two deliberately-disallowed types used by the negative entries.
_PDF = "application/pdf"
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_TXT = "text/plain"
_MD = "text/markdown"
_CSV = "text/csv"  # NOT allowlisted — negative entry
_HTML = "text/html"  # NOT allowlisted — negative entry

SizeClass = Literal["tiny", "small", "medium", "large"]
ExpectedIngest = Literal["ok", "rejected_type"]
TextQuality = Literal["good", "poor"]


@dataclass(frozen=True, slots=True)
class CorpusFile:
    """One pinned benchmark file: provenance, expectations, and download target.

    ``file_id`` is the stable handle questions reference; ``filename`` is the
    on-disk name inside the corpus dir. ``expected_ingest`` says what the
    pipeline should do with the file (``rejected_type`` = the format is outside
    the upload allowlist and parsing must fail closed). ``text_quality="poor"``
    marks a file whose bytes are valid but whose extractable text is known to be
    (near-)empty — the scanned-PDF limitation, pinned rather than papered over.
    ``smoke`` marks the one-per-format mini-subset cheap enough for a live run.
    """

    file_id: str
    filename: str
    url: str
    mime_type: str
    source: str
    license: str
    domain: str
    description: str
    language: str = "en"
    expected_ingest: ExpectedIngest = "ok"
    text_quality: TextQuality = "good"
    smoke: bool = False
    # Rolling entries (#443) point at a source's *current* alias (e.g. the IRS
    # current-year instructions) and are refreshed on demand: their checksum pin
    # is informational (last-seen), a changed upstream is expected rather than a
    # failure, and benchmark questions may never cite them (grounding stays on
    # immutable files only — enforced in bank/manifest validation).
    rolling: bool = False
    notes: str = ""


def size_class(num_bytes: int) -> SizeClass:
    """Bucket a pinned byte size into the ladder the README documents."""
    if num_bytes < 100_000:
        return "tiny"
    if num_bytes < 1_000_000:
        return "small"
    if num_bytes < 10_000_000:
        return "medium"
    return "large"


CORPUS: tuple[CorpusFile, ...] = (
    # --- text/plain -----------------------------------------------------------
    CorpusFile(
        file_id="gutenberg-pride-prejudice",
        filename="pride-and-prejudice.txt",
        url="https://www.gutenberg.org/cache/epub/1342/pg1342.txt",
        mime_type=_TXT,
        source="Project Gutenberg #1342 — Jane Austen, Pride and Prejudice",
        license="Public domain (US); Project Gutenberg License for the trademark",
        domain="literature",
        description="Full novel, plain text. Long-form narrative prose.",
        notes="Distractor pair with gutenberg-persuasion (same author/style).",
    ),
    CorpusFile(
        file_id="gutenberg-persuasion",
        filename="persuasion.txt",
        url="https://www.gutenberg.org/cache/epub/105/pg105.txt",
        mime_type=_TXT,
        source="Project Gutenberg #105 — Jane Austen, Persuasion",
        license="Public domain (US); Project Gutenberg License for the trademark",
        domain="literature",
        description="Full novel, plain text. Near-miss distractor for Pride and Prejudice.",
    ),
    CorpusFile(
        file_id="gutenberg-moby-dick",
        filename="moby-dick.txt",
        url="https://www.gutenberg.org/cache/epub/2701/pg2701.txt",
        mime_type=_TXT,
        source="Project Gutenberg #2701 — Herman Melville, Moby-Dick; or, The Whale",
        license="Public domain (US); Project Gutenberg License for the trademark",
        domain="literature",
        description="Full novel, plain text, ~1.3 MB — a medium-size single text document.",
    ),
    CorpusFile(
        file_id="gutenberg-shakespeare",
        filename="shakespeare-complete-works.txt",
        url="https://www.gutenberg.org/cache/epub/100/pg100.txt",
        mime_type=_TXT,
        source="Project Gutenberg #100 — The Complete Works of William Shakespeare",
        license="Public domain (US); Project Gutenberg License for the trademark",
        domain="literature",
        description="All plays and sonnets in one ~5.6 MB text file — the largest txt entry.",
        notes="Stress case: one file that chunks into thousands of passages.",
    ),
    CorpusFile(
        file_id="gutenberg-faust-de",
        filename="faust-der-tragoedie-erster-teil.txt",
        url="https://www.gutenberg.org/cache/epub/2229/pg2229.txt",
        mime_type=_TXT,
        source="Project Gutenberg #2229 — Goethe, Faust: Der Tragödie erster Teil",
        license="Public domain; Project Gutenberg License for the trademark",
        domain="literature",
        description="German verse drama, plain text — the non-English retrieval case.",
        language="de",
    ),
    CorpusFile(
        file_id="rfc9110-http-semantics",
        filename="rfc9110-http-semantics.txt",
        url="https://www.rfc-editor.org/rfc/rfc9110.txt",
        mime_type=_TXT,
        source="IETF RFC Editor — RFC 9110, HTTP Semantics (June 2022)",
        license="IETF Trust Legal Provisions (reproduction permitted)",
        domain="web-standards",
        description="Normative HTTP spec: status codes, methods, header semantics.",
        notes="Distractor pair with rfc9112 (adjacent HTTP specs). RFCs are immutable.",
    ),
    CorpusFile(
        file_id="rfc9112-http11",
        filename="rfc9112-http11.txt",
        url="https://www.rfc-editor.org/rfc/rfc9112.txt",
        mime_type=_TXT,
        source="IETF RFC Editor — RFC 9112, HTTP/1.1 (June 2022)",
        license="IETF Trust Legal Provisions (reproduction permitted)",
        domain="web-standards",
        description="HTTP/1.1 message syntax and connection management.",
        smoke=True,
    ),
    # --- text/markdown --------------------------------------------------------
    CorpusFile(
        file_id="pandoc-manual",
        filename="pandoc-manual.md",
        url="https://raw.githubusercontent.com/jgm/pandoc/3.6.3/MANUAL.txt",
        mime_type=_MD,
        source="Pandoc 3.6.3 user manual (tag-pinned raw from jgm/pandoc)",
        license="GPL-2.0-or-later (documentation of the pandoc project)",
        domain="software",
        description="Large real markdown manual: options, defaults, extension tables.",
        notes="Upstream file is MANUAL.txt but the content is pandoc markdown; "
        "stored as .md so it ingests as text/markdown. Tag-pinned → immutable.",
    ),
    CorpusFile(
        file_id="kubernetes-changelog-1-31",
        filename="kubernetes-changelog-1.31.md",
        url=(
            "https://raw.githubusercontent.com/kubernetes/kubernetes/v1.31.0/"
            "CHANGELOG/CHANGELOG-1.31.md"
        ),
        mime_type=_MD,
        source="Kubernetes v1.31.0 changelog (tag-pinned raw from kubernetes/kubernetes)",
        license="Apache-2.0",
        domain="software",
        description="Release changelog: dense exact tokens (versions, CVE ids, flags).",
        notes="Rare-token / keyword-retrieval material. Tag-pinned → immutable.",
    ),
    CorpusFile(
        file_id="fastapi-readme",
        filename="fastapi-readme.md",
        url="https://raw.githubusercontent.com/fastapi/fastapi/0.115.6/README.md",
        mime_type=_MD,
        source="FastAPI 0.115.6 README (tag-pinned raw from fastapi/fastapi)",
        license="MIT",
        domain="software",
        description="Small real README with badges, code samples, and factual claims.",
        smoke=True,
    ),
    # --- application/pdf ------------------------------------------------------
    CorpusFile(
        file_id="arxiv-attention",
        filename="attention-is-all-you-need.pdf",
        url="https://arxiv.org/pdf/1706.03762v7",
        mime_type=_PDF,
        source="arXiv:1706.03762v7 — Vaswani et al., Attention Is All You Need",
        license="arXiv.org perpetual non-exclusive license (local benchmarking use)",
        domain="ml-research",
        description="Seminal ML paper: figures, tables of BLEU scores, hyperparameters.",
        notes="Version-pinned arXiv URL → immutable. Numeric-lookup material.",
    ),
    CorpusFile(
        file_id="arxiv-bert",
        filename="bert-pretraining.pdf",
        url="https://arxiv.org/pdf/1810.04805v2",
        mime_type=_PDF,
        source="arXiv:1810.04805v2 — Devlin et al., BERT: Pre-training of Deep "
        "Bidirectional Transformers",
        license="arXiv.org perpetual non-exclusive license (local benchmarking use)",
        domain="ml-research",
        description="ML paper; cross-document pair with the Transformer paper.",
        smoke=True,
    ),
    CorpusFile(
        file_id="irs-1040-instructions-2024",
        filename="irs-form-1040-instructions-2024.pdf",
        url="https://www.irs.gov/pub/irs-prior/i1040gi--2024.pdf",
        mime_type=_PDF,
        source="IRS — Form 1040 instructions, tax year 2024 (prior-year archive)",
        license="Public domain (US federal government work)",
        domain="tax",
        description="Long government PDF full of tables, thresholds, and worksheets.",
        notes="Prior-year archive URL → stable, unlike the current-year alias.",
    ),
    CorpusFile(
        file_id="nist-sp-800-63b",
        filename="nist-sp-800-63b.pdf",
        url="https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63b.pdf",
        mime_type=_PDF,
        source="NIST SP 800-63B — Digital Identity Guidelines: Authentication",
        license="Public domain (US federal government work)",
        domain="security",
        description="Normative security guideline (SHALL/SHOULD requirements).",
    ),
    CorpusFile(
        file_id="ipcc-ar6-wg1-spm",
        filename="ipcc-ar6-wg1-spm.pdf",
        url="https://www.ipcc.ch/report/ar6/wg1/downloads/report/IPCC_AR6_WGI_SPM.pdf",
        mime_type=_PDF,
        source="IPCC AR6 WGI — Summary for Policymakers (2021)",
        license="© IPCC; reproduction for non-commercial purposes permitted with attribution",
        domain="climate",
        description="Dense scientific summary with headline numeric findings.",
        notes="Cross-document pair with ipcc-ar6-wg1-chapter02.",
    ),
    CorpusFile(
        file_id="ipcc-ar6-wg1-chapter02",
        filename="ipcc-ar6-wg1-chapter02.pdf",
        url="https://www.ipcc.ch/report/ar6/wg1/downloads/report/IPCC_AR6_WGI_Chapter02.pdf",
        mime_type=_PDF,
        source="IPCC AR6 WGI — Chapter 2: Changing State of the Climate System (2021)",
        license="© IPCC; reproduction for non-commercial purposes permitted with attribution",
        domain="climate",
        description="~26 MB, 200+ page chapter — the large-PDF ingestion stress case.",
    ),
    CorpusFile(
        file_id="census-statistical-abstract-2012",
        filename="us-statistical-abstract-2012.pdf",
        url="https://www2.census.gov/library/publications/2011/compendia/statab/131ed/2012-statab.pdf",
        mime_type=_PDF,
        source="US Census Bureau — Statistical Abstract of the United States: 2012 (131st ed.)",
        license="Public domain (US federal government work)",
        domain="statistics",
        description="~32 MB, ~1000 pages of statistical tables — the largest corpus entry.",
        notes="Table-heavy large PDF; pairs with the XLSX entries for table questions.",
    ),
    CorpusFile(
        file_id="irs-form-1040-1913",
        filename="irs-form-1040-1913.pdf",
        url="https://www.irs.gov/pub/irs-prior/f1040--1913.pdf",
        mime_type=_PDF,
        source="IRS — the original 1913 Form 1040 (historical document, re-typeset by the IRS)",
        license="Public domain (US federal government work)",
        domain="tax",
        description="Historical form PDF with sparse, form-layout text (incl. the IRS's "
        "print-spec header) — a messy-but-real extraction case.",
        notes="Not an image-only scan: the IRS publishes it with a text layer, so it "
        "extracts real (if oddly ordered) text.",
    ),
    # --- Industry documents (#430) --------------------------------------------
    # Real business/industry publishing is PDF-dominant, so this slice is all
    # PDF: finance, cloud architecture, standards, pharmaceutical, and
    # semiconductor docs from stable vendor/organisation hosts. SEC EDGAR was
    # evaluated for an industry XLSX and is excluded: it 403s scripted access
    # from this environment even with its policy-compliant contact User-Agent
    # (same exclusion policy as cdc.gov / bls.gov / govinfo.gov).
    CorpusFile(
        file_id="berkshire-2023-letter",
        filename="berkshire-2023-shareholder-letter.pdf",
        url="https://www.berkshirehathaway.com/letters/2023ltr.pdf",
        mime_type=_PDF,
        source="Berkshire Hathaway — Warren Buffett's 2023 letter to shareholders",
        license="© Berkshire Hathaway; public investor-relations document (local "
        "benchmarking use only)",
        domain="finance",
        description="Classic industry document: shareholder letter with GAAP figures "
        "and plain-English financial commentary.",
    ),
    CorpusFile(
        file_id="aws-well-architected",
        filename="aws-well-architected-framework.pdf",
        url=(
            "https://docs.aws.amazon.com/pdfs/wellarchitected/latest/framework/"
            "wellarchitected-framework.pdf"
        ),
        mime_type=_PDF,
        source="Amazon Web Services — Well-Architected Framework whitepaper",
        license="© Amazon; public documentation (local benchmarking use only)",
        domain="cloud-architecture",
        description="~14 MB vendor architecture whitepaper — the large industry-PDF "
        "case; pillar structure suits set/list questions.",
    ),
    CorpusFile(
        file_id="ecma-404-json",
        filename="ecma-404-json-syntax.pdf",
        url=(
            "https://ecma-international.org/wp-content/uploads/"
            "ECMA-404_2nd_edition_december_2017.pdf"
        ),
        mime_type=_PDF,
        source="Ecma International — ECMA-404, The JSON Data Interchange Syntax (2nd ed.)",
        license="© Ecma International; standards available free of charge",
        domain="standards",
        description="Short formal industry standard — precise normative definitions.",
    ),
    CorpusFile(
        file_id="fda-metformin-label",
        filename="fda-glucophage-prescribing-information.pdf",
        url=(
            "https://www.accessdata.fda.gov/drugsatfda_docs/label/2017/"
            "020357s037s039,021202s021s023lbl.pdf"
        ),
        mime_type=_PDF,
        source="FDA (Drugs@FDA) — Glucophage/Glucophage XR (metformin) prescribing " "information",
        license="Public domain (US federal government work / approved labeling)",
        domain="pharmaceutical",
        description="Pharma industry labeling: dosing tables, contraindications, "
        "boxed warning — dense regulated-industry prose.",
        notes="accessdata.fda.gov accepts the project UA (unlike cdc.gov).",
    ),
    CorpusFile(
        file_id="intel-sdm-vol1",
        filename="intel-sdm-volume-1.pdf",
        url="https://cdrdv2-public.intel.com/671436/253665-sdm-vol-1.pdf",
        mime_type=_PDF,
        source="Intel — 64 and IA-32 Architectures Software Developer's Manual, Vol. 1",
        license="© Intel; public developer documentation (local benchmarking use only)",
        domain="semiconductor",
        description="Hardware-industry reference manual: registers, modes, "
        "instruction-set architecture basics.",
    ),
    # --- DOCX -----------------------------------------------------------------
    CorpusFile(
        file_id="nidcr-monitoring-guidelines",
        filename="nidcr-clinical-monitoring-guidelines.docx",
        url=(
            "https://www.nidcr.nih.gov/sites/default/files/2017-12/"
            "nidcr-clinical-monitoring-guidelines.docx"
        ),
        mime_type=_DOCX,
        source="NIH NIDCR — Clinical Monitoring Guidelines",
        license="Public domain (US federal government work)",
        domain="clinical-research",
        description="Real prose-heavy Word guidance document (monitoring processes).",
        smoke=True,
    ),
    CorpusFile(
        file_id="nidcr-monitoring-plan-template",
        filename="nidcr-clinical-monitoring-plan-template.docx",
        url=(
            "https://www.nidcr.nih.gov/sites/default/files/2018-03/"
            "clinical-monitoring-plan-template_0.docx"
        ),
        mime_type=_DOCX,
        source="NIH NIDCR — Clinical Monitoring Plan template",
        license="Public domain (US federal government work)",
        domain="clinical-research",
        description="Word template with draft language and tables.",
    ),
    CorpusFile(
        file_id="nidcr-site-activation-checklist",
        filename="nidcr-site-activation-checklist.docx",
        url=(
            "https://www.nidcr.nih.gov/sites/default/files/2026-02/"
            "Extramural-Site-Activation-Ref-List.docx"
        ),
        mime_type=_DOCX,
        source="NIH NIDCR — Extramural Site Activation Reference List",
        license="Public domain (US federal government work)",
        domain="clinical-research",
        description="Checklist DOCX whose content lives in Word tables — the "
        "paragraphs-only DOCX parser extracts almost nothing from it.",
        text_quality="poor",
        notes="Pins a real parser limitation (#21): python-docx paragraph iteration "
        "skips table cells, so table-heavy Word docs are invisible to retrieval. "
        "Follow-up filed to extract DOCX table text. No questions reference this file.",
    ),
    # --- PPTX -----------------------------------------------------------------
    CorpusFile(
        file_id="unsd-data-collection-tech",
        filename="unsd-electronic-data-collection.pptx",
        url=(
            "https://unstats.un.org/unsd/demographic-social/meetings/2017/"
            "lagos--regional-workshop-on-2020-census/docs/s11-01-UNSD.pptx"
        ),
        mime_type=_PPTX,
        source="UN Statistics Division — Planning for adoption of electronic data "
        "collection technologies (2017 census workshop, session 11)",
        license="© United Nations; workshop material published for public download",
        domain="demography",
        description="Real training deck: pros/cons of electronic census data collection.",
        smoke=True,
    ),
    CorpusFile(
        file_id="unsd-trade-in-services",
        filename="unsd-trade-in-services.pptx",
        url=(
            "https://unstats.un.org/unsd/trade/events/2021/Beijing_workshop/"
            "presentations/3_1_Overview_trade_in_services_Carboni.pptx"
        ),
        mime_type=_PPTX,
        source="UN Statistics Division — Overview of trade in services "
        "(2021 Beijing workshop deck)",
        license="© United Nations; workshop material published for public download",
        domain="trade",
        description="Slide deck on trade-in-services statistics concepts.",
    ),
    # --- XLSX -----------------------------------------------------------------
    CorpusFile(
        file_id="census-state-population",
        filename="census-state-population-2020-2024.xlsx",
        url=(
            "https://www2.census.gov/programs-surveys/popest/tables/2020-2024/"
            "state/totals/NST-EST2024-POP.xlsx"
        ),
        mime_type=_XLSX,
        source="US Census Bureau — Annual state resident population estimates 2020–2024 "
        "(NST-EST2024-POP)",
        license="Public domain (US federal government work)",
        domain="statistics",
        description="Small real workbook: one table of state population estimates.",
        smoke=True,
        notes="Aggregation-question material (max/compare across rows).",
    ),
    CorpusFile(
        file_id="census-county-population",
        filename="census-county-population-2020-2024.xlsx",
        url=(
            "https://www2.census.gov/programs-surveys/popest/tables/2020-2024/"
            "counties/totals/co-est2024-pop.xlsx"
        ),
        mime_type=_XLSX,
        source="US Census Bureau — Annual county resident population estimates 2020–2024 "
        "(CO-EST2024-POP)",
        license="Public domain (US federal government work)",
        domain="statistics",
        description="~3k-row workbook — the larger spreadsheet extraction case.",
    ),
    # --- Industry-pack additions (#443) ---------------------------------------
    # Files added to give the healthcare / financial / legal packs real
    # substance (see packs.py for the research-backed pack definitions).
    CorpusFile(
        file_id="fda-lipitor-label",
        filename="fda-lipitor-prescribing-information.pdf",
        url="https://www.accessdata.fda.gov/drugsatfda_docs/label/2019/020702s073lbl.pdf",
        mime_type=_PDF,
        source="FDA (Drugs@FDA) — Lipitor (atorvastatin calcium) prescribing information",
        license="Public domain (US federal government work / approved labeling)",
        domain="pharmaceutical",
        description="Second drug label: dosing, interactions, trial tables — pairs "
        "with the metformin label for within-domain retrieval discrimination.",
    ),
    CorpusFile(
        file_id="berkshire-2022-letter",
        filename="berkshire-2022-shareholder-letter.pdf",
        url="https://www.berkshirehathaway.com/letters/2022ltr.pdf",
        mime_type=_PDF,
        source="Berkshire Hathaway — Warren Buffett's 2022 letter to shareholders",
        license="© Berkshire Hathaway; public investor-relations document (local "
        "benchmarking use only)",
        domain="finance",
        description="Prior-year letter — year-over-year pair with the 2023 letter.",
    ),
    CorpusFile(
        file_id="bis-basel3-finalisation",
        filename="bis-basel3-finalisation.pdf",
        url="https://www.bis.org/bcbs/publ/d424.pdf",
        mime_type=_PDF,
        source="Bank for International Settlements — Basel III: Finalising "
        "post-crisis reforms (d424)",
        license="© BIS; reproduction/translation permitted with attribution",
        domain="finance",
        description="Banking regulation: capital-requirement rules and ratios — the "
        "regulatory backbone financial-services teams query.",
    ),
    CorpusFile(
        file_id="eurlex-gdpr",
        filename="eu-gdpr-regulation-2016-679.pdf",
        url="https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:32016R0679",
        mime_type=_PDF,
        source="EUR-Lex — Regulation (EU) 2016/679 (GDPR), English PDF",
        license="© European Union; reuse permitted (Commission Decision 2011/833/EU)",
        domain="legal",
        description="The privacy regulation legal/compliance teams query daily: "
        "articles, recitals, defined terms.",
    ),
    CorpusFile(
        file_id="copyright-circular-1",
        filename="us-copyright-circular-1.pdf",
        url="https://www.copyright.gov/circs/circ01.pdf",
        mime_type=_PDF,
        source="US Copyright Office — Circular 1: Copyright Basics",
        license="Public domain (US federal government work)",
        domain="legal",
        description="Plain-language legal guidance: registration, duration, fair use.",
    ),
    CorpusFile(
        file_id="gutenberg-us-constitution",
        filename="us-constitution.txt",
        url="https://www.gutenberg.org/cache/epub/5/pg5.txt",
        mime_type=_TXT,
        source="Project Gutenberg #5 — The United States Constitution",
        license="Public domain; Project Gutenberg License for the trademark",
        domain="legal",
        description="Foundational legal text: articles and amendments.",
    ),
    CorpusFile(
        file_id="gutenberg-federalist-papers",
        filename="federalist-papers.txt",
        url="https://www.gutenberg.org/cache/epub/1404/pg1404.txt",
        mime_type=_TXT,
        source="Project Gutenberg #1404 — The Federalist Papers",
        license="Public domain; Project Gutenberg License for the trademark",
        domain="legal",
        description="85 essays of constitutional interpretation — long-form legal "
        "argumentation to retrieve across.",
    ),
    # --- Rolling entries (#443): refresh-on-demand aliases --------------------
    CorpusFile(
        file_id="irs-1040-instructions-current",
        filename="irs-form-1040-instructions-current.pdf",
        url="https://www.irs.gov/pub/irs-pdf/i1040gi.pdf",
        mime_type=_PDF,
        source="IRS — Form 1040 instructions, CURRENT tax year (rolling alias)",
        license="Public domain (US federal government work)",
        domain="tax",
        description="The current-year instructions alias the IRS re-publishes "
        "annually — the refresh-on-demand case (loader --refresh).",
        rolling=True,
        notes="Deliberately unpinnable: the URL's content changes every filing "
        "season. The pinned prior-year archive entry stays the benchmark's "
        "immutable ground truth; no questions may cite this file.",
    ),
    # --- Tax research: US federal (IRS) ---------------------------------------
    # Filing guidance for the New York pack's federal layer (a New York filer
    # always files federally too). Year-stamped entries come from the IRS
    # *prior-year archive* (``/pub/irs-prior/<code>--<year>.pdf``), which is
    # immutable; the two publications the IRS revises without year-stamping are
    # marked ``rolling`` and fetched from the current alias instead.
    CorpusFile(
        file_id="irs-pub17-individual-2024",
        filename="irs-pub17-your-federal-income-tax-2024.pdf",
        url="https://www.irs.gov/pub/irs-prior/p17--2024.pdf",
        mime_type=_PDF,
        source="IRS Publication 17 — Your Federal Income Tax, tax year 2024",
        license="Public domain (US federal government work)",
        domain="tax",
        description="The individual filer's reference manual: income, deductions, "
        "credits, filing status, and dependents in one long PDF.",
    ),
    CorpusFile(
        file_id="irs-i1120-corporation-2024",
        filename="irs-form-1120-instructions-2024.pdf",
        url="https://www.irs.gov/pub/irs-prior/i1120--2024.pdf",
        mime_type=_PDF,
        source="IRS — Form 1120 (US Corporation Income Tax Return) instructions, 2024",
        license="Public domain (US federal government work)",
        domain="tax",
        description="Corporate return instructions: schedules, due dates, and the "
        "line-by-line computation of taxable income.",
    ),
    CorpusFile(
        file_id="irs-i1065-partnership-2024",
        filename="irs-form-1065-instructions-2024.pdf",
        url="https://www.irs.gov/pub/irs-prior/i1065--2024.pdf",
        mime_type=_PDF,
        source="IRS — Form 1065 (US Return of Partnership Income) instructions, 2024",
        license="Public domain (US federal government work)",
        domain="tax",
        description="Partnership return instructions, including Schedule K-1 "
        "reporting for each partner's distributive share.",
    ),
    CorpusFile(
        file_id="irs-i1120s-s-corporation-2024",
        filename="irs-form-1120s-instructions-2024.pdf",
        url="https://www.irs.gov/pub/irs-prior/i1120s--2024.pdf",
        mime_type=_PDF,
        source="IRS — Form 1120-S (S Corporation Income Tax Return) instructions, 2024",
        license="Public domain (US federal government work)",
        domain="tax",
        description="S-corporation return instructions: shareholder basis, "
        "built-in gains, and pass-through reporting.",
        notes="Distractor pair with irs-i1065-partnership-2024 — adjacent "
        "pass-through regimes whose rules differ.",
    ),
    CorpusFile(
        file_id="irs-i1040sc-schedule-c-2024",
        filename="irs-schedule-c-instructions-2024.pdf",
        url="https://www.irs.gov/pub/irs-prior/i1040sc--2024.pdf",
        mime_type=_PDF,
        source="IRS — Schedule C (Profit or Loss From Business) instructions, 2024",
        license="Public domain (US federal government work)",
        domain="tax",
        description="Sole-proprietor business income: expense categories, "
        "business-use-of-home, and the material-participation tests.",
    ),
    CorpusFile(
        file_id="irs-pub15-circular-e-2024",
        filename="irs-pub15-circular-e-employers-tax-guide-2024.pdf",
        url="https://www.irs.gov/pub/irs-prior/p15--2024.pdf",
        mime_type=_PDF,
        source="IRS Publication 15 (Circular E) — Employer's Tax Guide, 2024",
        license="Public domain (US federal government work)",
        domain="tax",
        description="Employer payroll obligations: withholding, FICA, deposit "
        "schedules, and the penalty rules for late deposits.",
    ),
    CorpusFile(
        file_id="irs-iw2w3-wage-statements-2024",
        filename="irs-form-w2-w3-instructions-2024.pdf",
        url="https://www.irs.gov/pub/irs-prior/iw2w3--2024.pdf",
        mime_type=_PDF,
        source="IRS — General Instructions for Forms W-2 and W-3, 2024",
        license="Public domain (US federal government work)",
        domain="tax",
        description="Wage-statement reporting: box-by-box codes, correction "
        "procedure, and information-return penalties.",
    ),
    CorpusFile(
        file_id="irs-pub946-depreciation-2024",
        filename="irs-pub946-how-to-depreciate-property-2024.pdf",
        url="https://www.irs.gov/pub/irs-prior/p946--2024.pdf",
        mime_type=_PDF,
        source="IRS Publication 946 — How To Depreciate Property, 2024",
        license="Public domain (US federal government work)",
        domain="tax",
        description="MACRS recovery periods, section 179 expensing, bonus "
        "depreciation, and the percentage tables.",
    ),
    CorpusFile(
        file_id="irs-pub505-estimated-tax-2024",
        filename="irs-pub505-tax-withholding-and-estimated-tax-2024.pdf",
        url="https://www.irs.gov/pub/irs-prior/p505--2024.pdf",
        mime_type=_PDF,
        source="IRS Publication 505 — Tax Withholding and Estimated Tax, 2024",
        license="Public domain (US federal government work)",
        domain="tax",
        description="Quarterly instalments, safe-harbour percentages, and the "
        "underpayment-penalty computation.",
    ),
    CorpusFile(
        file_id="irs-pub519-aliens-2024",
        filename="irs-pub519-us-tax-guide-for-aliens-2024.pdf",
        url="https://www.irs.gov/pub/irs-prior/p519--2024.pdf",
        mime_type=_PDF,
        source="IRS Publication 519 — US Tax Guide for Aliens, 2024",
        license="Public domain (US federal government work)",
        domain="tax",
        description="Residency tests (substantial presence, green card), "
        "dual-status years, and treaty-based return positions.",
    ),
    CorpusFile(
        file_id="irs-i1041-estates-trusts-2024",
        filename="irs-form-1041-instructions-2024.pdf",
        url="https://www.irs.gov/pub/irs-prior/i1041--2024.pdf",
        mime_type=_PDF,
        source="IRS — Form 1041 (Income Tax Return for Estates and Trusts) " "instructions, 2024",
        license="Public domain (US federal government work)",
        domain="tax",
        description="Fiduciary return instructions: distributable net income, "
        "the 65-day rule, and Schedule K-1 beneficiary reporting.",
    ),
    CorpusFile(
        file_id="irs-irb-2025-01",
        filename="irs-internal-revenue-bulletin-2025-01.pdf",
        url="https://www.irs.gov/pub/irs-irbs/irb25-01.pdf",
        mime_type=_PDF,
        source="IRS — Internal Revenue Bulletin No. 2025-1 (30 December 2024)",
        license="Public domain (US federal government work)",
        domain="tax",
        description="Primary authority as tax researchers cite it: revenue "
        "rulings, revenue procedures, notices, and Treasury decisions — including "
        "Rev. Proc. 2025-1 on letter rulings.",
        notes="Bulletins are published once and never revised — a naturally immutable URL.",
    ),
    CorpusFile(
        file_id="irs-rev-proc-2024-40",
        filename="irs-rev-proc-2024-40-inflation-adjustments.pdf",
        url="https://www.irs.gov/pub/irs-drop/rp-24-40.pdf",
        mime_type=_PDF,
        source="IRS — Revenue Procedure 2024-40, inflation-adjusted amounts for 2025",
        license="Public domain (US federal government work)",
        domain="tax",
        description="The annual indexation tables: bracket thresholds, standard "
        "deduction, and dozens of dollar limits for tax year 2025.",
        notes="Dense exact-figure tables — the lookup shape tax questions take.",
    ),
    CorpusFile(
        file_id="irs-soi-new-york-2022",
        filename="irs-soi-new-york-individual-income-tax-2022.xlsx",
        url="https://www.irs.gov/pub/irs-soi/22in33ny.xlsx",
        mime_type=_XLSX,
        source="IRS Statistics of Income — New York individual income tax returns, 2022",
        license="Public domain (US federal government work)",
        domain="tax",
        description="Real tax spreadsheet: New York returns by AGI band, with "
        "counts and dollar amounts per line item.",
        notes="The tax packs' aggregation/table case — a genuine XLSX, not prose.",
    ),
    CorpusFile(
        file_id="irs-pub556-appeals-current",
        filename="irs-pub556-examination-appeal-rights-current.pdf",
        url="https://www.irs.gov/pub/irs-pdf/p556.pdf",
        mime_type=_PDF,
        source="IRS Publication 556 — Examination of Returns, Appeal Rights, and "
        "Claims for Refund (current revision)",
        license="Public domain (US federal government work)",
        domain="tax",
        description="Audit process, the 30-day letter, Appeals conferences, and "
        "refund-claim limitation periods.",
        rolling=True,
        notes="Revision-dated rather than year-stamped: the IRS has no "
        "prior-year archive URL for it, so the current alias is the only "
        "citable source — refreshed on demand.",
    ),
    CorpusFile(
        file_id="irs-pub1-taxpayer-rights-current",
        filename="irs-pub1-your-rights-as-a-taxpayer-current.pdf",
        url="https://www.irs.gov/pub/irs-pdf/p1.pdf",
        mime_type=_PDF,
        source="IRS Publication 1 — Your Rights as a Taxpayer (current revision)",
        license="Public domain (US federal government work)",
        domain="tax",
        description="The Taxpayer Bill of Rights: ten enumerated rights plus the "
        "examination, collection, and appeal safeguards.",
        rolling=True,
        notes="Revision-dated, not year-stamped — see irs-pub556-appeals-current.",
    ),
    # --- Tax research: New York State ----------------------------------------
    # NYS publishes annual income/corporation forms into an immutable per-year
    # archive (``/pdf/<year>/inc|corp/<form>_<year>.pdf``) — those are pinned.
    # Sales-tax, withholding, estate and property documents live only at their
    # current path and are re-published in place, so they are ``rolling``.
    # NYC's own business taxes are deliberately absent: nyc.gov answers 403 to
    # our honest User-Agent, and the corpus policy is to exclude bot-walled
    # hosts rather than evade them.
    CorpusFile(
        file_id="nys-it201i-resident-2024",
        filename="nys-it201-resident-income-tax-instructions-2024.pdf",
        url="https://www.tax.ny.gov/pdf/2024/inc/it201i_2024.pdf",
        mime_type=_PDF,
        source="NYS Department of Taxation and Finance — Form IT-201-I, "
        "Resident Income Tax Return instructions, 2024",
        license="New York State government work (freely distributable tax form)",
        domain="tax",
        description="The New York resident return: state tax tables, NYC and "
        "Yonkers resident surcharges, and the state credit schedule.",
        notes="Distractor pair with nys-it203i-nonresident-2024 — resident vs "
        "nonresident rules diverge on income allocation.",
    ),
    CorpusFile(
        file_id="nys-it203i-nonresident-2024",
        filename="nys-it203-nonresident-income-tax-instructions-2024.pdf",
        url="https://www.tax.ny.gov/pdf/2024/inc/it203i_2024.pdf",
        mime_type=_PDF,
        source="NYS Department of Taxation and Finance — Form IT-203-I, "
        "Nonresident and Part-Year Resident Income Tax Return instructions, 2024",
        license="New York State government work (freely distributable tax form)",
        domain="tax",
        description="New York source income allocation for nonresidents and "
        "part-year residents — the commuter and relocation case.",
    ),
    CorpusFile(
        file_id="nys-it204i-partnership-2024",
        filename="nys-it204-partnership-return-instructions-2024.pdf",
        url="https://www.tax.ny.gov/pdf/2024/inc/it204i_2024.pdf",
        mime_type=_PDF,
        source="NYS Department of Taxation and Finance — Form IT-204-I, "
        "Partnership Return instructions, 2024",
        license="New York State government work (freely distributable tax form)",
        domain="tax",
        description="Partnership filing duty, New York apportionment, and the "
        "partner schedules a New York partnership must issue.",
    ),
    CorpusFile(
        file_id="nys-ct3i-franchise-2024",
        filename="nys-ct3-general-business-franchise-tax-instructions-2024.pdf",
        url="https://www.tax.ny.gov/pdf/2024/corp/ct3i_2024.pdf",
        mime_type=_PDF,
        source="NYS Department of Taxation and Finance — Form CT-3-I, General "
        "Business Corporation Franchise Tax Return instructions, 2024",
        license="New York State government work (freely distributable tax form)",
        domain="tax",
        description="Post-reform New York franchise tax: the business income "
        "base, capital base, fixed dollar minimum, and apportionment.",
    ),
    CorpusFile(
        file_id="nys-ct3si-s-corporation-2024",
        filename="nys-ct3s-new-york-s-corporation-instructions-2024.pdf",
        url="https://www.tax.ny.gov/pdf/2024/corp/ct3si_2024.pdf",
        mime_type=_PDF,
        source="NYS Department of Taxation and Finance — Form CT-3-S-I, New York "
        "S Corporation Franchise Tax Return instructions, 2024",
        license="New York State government work (freely distributable tax form)",
        domain="tax",
        description="New York S-corporation election, the fixed dollar minimum, "
        "and shareholder pass-through reporting.",
    ),
    CorpusFile(
        file_id="nys-it225i-modifications-2024",
        filename="nys-it225-state-modifications-instructions-2024.pdf",
        url="https://www.tax.ny.gov/pdf/2024/inc/it225i_2024.pdf",
        mime_type=_PDF,
        source="NYS Department of Taxation and Finance — Form IT-225-I, New York "
        "State Modifications instructions, 2024",
        license="New York State government work (freely distributable tax form)",
        domain="tax",
        description="Every addition and subtraction modification code that moves "
        "federal income to New York income — a pure code-lookup document.",
    ),
    CorpusFile(
        file_id="nys-it112ri-resident-credit-2024",
        filename="nys-it112r-resident-credit-instructions-2024.pdf",
        url="https://www.tax.ny.gov/pdf/2024/inc/it112ri_2024.pdf",
        mime_type=_PDF,
        source="NYS Department of Taxation and Finance — Form IT-112-R-I, New York "
        "State Resident Credit instructions, 2024",
        license="New York State government work (freely distributable tax form)",
        domain="tax",
        description="Credit for income tax paid to another state — the "
        "double-taxation relief computation for cross-border earners.",
    ),
    CorpusFile(
        file_id="nys-it2105i-estimated-tax-2024",
        filename="nys-it2105-estimated-income-tax-instructions-2024.pdf",
        url="https://www.tax.ny.gov/pdf/2024/inc/it2105i_2024.pdf",
        mime_type=_PDF,
        source="NYS Department of Taxation and Finance — Form IT-2105-I, Estimated "
        "Tax Payment instructions for individuals, 2024",
        license="New York State government work (freely distributable tax form)",
        domain="tax",
        description="New York instalment due dates, the annualized-income option, "
        "and estimated-tax penalty exceptions.",
    ),
    CorpusFile(
        file_id="nys-tsbm-ptet-2021",
        filename="nys-tsb-m-21-1c-1i-pass-through-entity-tax.pdf",
        url="https://www.tax.ny.gov/pdf/memos/ptet/m21-1c-1i.pdf",
        mime_type=_PDF,
        source="NYS Department of Taxation and Finance — TSB-M-21(1)C, (1)I, "
        "Pass-Through Entity Tax (25 August 2021)",
        license="New York State government work (official technical memorandum)",
        domain="tax",
        description="The Department's own interpretation of Article 24-A PTET: "
        "who may elect, the irrevocable annual election, and the partner credit.",
        notes="A technical memorandum is dated and never revised in place — "
        "immutable, and the closest thing to citable state authority.",
    ),
    CorpusFile(
        file_id="nys-pub750-sales-tax-current",
        filename="nys-pub750-guide-to-sales-tax-current.pdf",
        url="https://www.tax.ny.gov/pdf/publications/sales/pub750.pdf",
        mime_type=_PDF,
        source="NYS Publication 750 — A Guide to Sales Tax in New York State " "(current revision)",
        license="New York State government work (freely distributable publication)",
        domain="tax",
        description="Vendor registration, what is taxable, exemption "
        "certificates, and the sales-tax filing calendar.",
        rolling=True,
        notes="Revision-dated at a fixed URL — re-published in place, so rolling.",
    ),
    CorpusFile(
        file_id="nys-pub718-sales-tax-rates-current",
        filename="nys-pub718-sales-tax-rates-by-jurisdiction-current.pdf",
        url="https://www.tax.ny.gov/pdf/publications/sales/pub718.pdf",
        mime_type=_PDF,
        source="NYS Publication 718 — New York State Sales and Use Tax Rates by "
        "Jurisdiction (current revision)",
        license="New York State government work (freely distributable publication)",
        domain="tax",
        description="Combined state/local rate and reporting code for every New "
        "York county and city — the rate-lookup table.",
        rolling=True,
        notes="Rates change by legislation; the URL always serves the current " "schedule.",
    ),
    CorpusFile(
        file_id="nys-st100i-sales-return-current",
        filename="nys-st100-quarterly-sales-tax-return-instructions-current.pdf",
        url="https://www.tax.ny.gov/pdf/current_forms/st/st100i.pdf",
        mime_type=_PDF,
        source="NYS Department of Taxation and Finance — Form ST-100-I, Quarterly "
        "Sales and Use Tax Return instructions (current period)",
        license="New York State government work (freely distributable tax form)",
        domain="tax",
        description="Quarterly sales-tax filing mechanics: jurisdiction reporting, "
        "vendor collection credit, and prepaid-tax schedules.",
        rolling=True,
        notes="The ``current_forms`` path is a per-period alias — rolling by " "construction.",
    ),
    CorpusFile(
        file_id="nys-nys45i-employer-quarterly-current",
        filename="nys-nys45-employer-quarterly-return-instructions-current.pdf",
        url="https://www.tax.ny.gov/pdf/current_forms/wt/nys45i.pdf",
        mime_type=_PDF,
        source="NYS Department of Taxation and Finance — Form NYS-45-I, Quarterly "
        "Combined Withholding, Wage Reporting and UI Return instructions (current)",
        license="New York State government work (freely distributable tax form)",
        domain="tax",
        description="The single New York employer return: unemployment insurance "
        "contributions, wage reporting, and withholding reconciliation.",
        rolling=True,
    ),
    CorpusFile(
        file_id="nys-nys50t-withholding-tables-current",
        filename="nys-nys50-t-nys-withholding-tax-tables-current.pdf",
        url="https://www.tax.ny.gov/pdf/publications/withholding/nys50_t_nys.pdf",
        mime_type=_PDF,
        source="NYS Publication NYS-50-T-NYS — New York State Withholding Tax "
        "Tables and Methods (current revision)",
        license="New York State government work (freely distributable publication)",
        domain="tax",
        description="Payroll withholding tables and the exact-calculation method "
        "by payroll frequency — dense numeric tables.",
        rolling=True,
    ),
    CorpusFile(
        file_id="nys-et706i-estate-tax-current",
        filename="nys-et706-estate-tax-return-instructions-current.pdf",
        url="https://www.tax.ny.gov/pdf/current_forms/et/et706i.pdf",
        mime_type=_PDF,
        source="NYS Department of Taxation and Finance — Form ET-706-I, New York "
        "State Estate Tax Return instructions (current revision)",
        license="New York State government work (freely distributable tax form)",
        domain="tax",
        description="New York estate tax: the basic exclusion amount, the "
        "three-year gift add-back, and the exclusion cliff.",
        rolling=True,
    ),
    CorpusFile(
        file_id="nys-tp584i-transfer-tax-current",
        filename="nys-tp584-real-estate-transfer-tax-instructions-current.pdf",
        url="https://www.tax.ny.gov/pdf/current_forms/property/tp584i.pdf",
        mime_type=_PDF,
        source="NYS Department of Taxation and Finance — Form TP-584-I, Real "
        "Estate Transfer Tax Return instructions (current revision)",
        license="New York State government work (freely distributable tax form)",
        domain="tax",
        description="Transfer tax on conveyances, the mansion tax, and the "
        "exemptions claimed at closing.",
        rolling=True,
    ),
    CorpusFile(
        file_id="nys-pub1093-veterans-exemption-current",
        filename="nys-pub1093-veterans-property-tax-exemption-current.pdf",
        url="https://www.tax.ny.gov/pdf/publications/orpts/pub1093.pdf",
        mime_type=_PDF,
        source="NYS Publication 1093 — Veterans Exemption Questions and Answers: "
        "Partial Exemption from Property Taxes in New York State (current revision)",
        license="New York State government work (freely distributable publication)",
        domain="tax",
        description="The three veterans' property-tax exemptions, their eligibility "
        "tests and application mechanics — the local-tax layer a New York filer "
        "also meets.",
        rolling=True,
    ),
    CorpusFile(
        file_id="nys-mta305i-mctmt-current",
        filename="nys-mta305-employer-mctmt-return-instructions-current.pdf",
        url="https://www.tax.ny.gov/pdf/current_forms/mctmt/mta305i.pdf",
        mime_type=_PDF,
        source="NYS Department of Taxation and Finance — Form MTA-305-I, Employer's "
        "Quarterly Metropolitan Commuter Transportation Mobility Tax Return "
        "instructions (current revision)",
        license="New York State government work (freely distributable tax form)",
        domain="tax",
        description="The MCTMT: which employers in the MCTD owe it, the two zones, "
        "the payroll-expense thresholds, and the rates.",
        rolling=True,
        notes="A New York-only tax with no federal analogue — the kind of "
        "jurisdiction-specific obligation a tax pack exists to surface. Chosen "
        "over Publication 420, whose own cover insert says its MCTMT computation "
        "guidance is out of date and cannot be relied upon.",
    ),
    # --- Tax research: Canada federal (CRA) + Ontario -------------------------
    # CRA guides carry the tax year in the filename (``t4012-25e.pdf``), so each
    # year is its own immutable URL — all pinned. The consolidated statutes at
    # Justice Laws and the Ontario Central Forms Repository serve *current* law
    # and forms in place, so those are ``rolling``. Ontario's own consolidated
    # statutes (e-Laws) are HTML-only, which is outside the upload allowlist —
    # federal statutes carry primary authority for the Ontario pack instead.
    CorpusFile(
        file_id="cra-5000-g-federal-guide-2025",
        filename="cra-5000-g-federal-income-tax-benefit-guide-2025.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/5000-g/5000-g-25e.pdf",
        mime_type=_PDF,
        source="CRA 5000-G — Federal Income Tax and Benefit Guide, 2025",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="The T1 line-by-line guide every Canadian filer works from, "
        "including what changed for the year.",
    ),
    CorpusFile(
        file_id="cra-5006-pc-ontario-guide-2025",
        filename="cra-5006-pc-ontario-information-guide-2025.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/5006-pc/5006-pc-25e.pdf",
        mime_type=_PDF,
        source="CRA 5006-PC — Ontario Information Guide (T1 Ontario package), 2025",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="Ontario-specific personal credits, the Ontario surtax, and "
        "the province's own benefit programs.",
    ),
    CorpusFile(
        file_id="cra-5006-c-on428-2025",
        filename="cra-5006-c-on428-ontario-tax-2025.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pbg/5006-c/5006-c-25e.pdf",
        mime_type=_PDF,
        source="CRA 5006-C — Form ON428, Ontario Tax, 2025",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="The Ontario tax calculation itself: bracket rates, surtax "
        "thresholds, and non-refundable credit amounts.",
    ),
    CorpusFile(
        file_id="cra-t4012-t2-corporation-2025",
        filename="cra-t4012-t2-corporation-income-tax-guide-2025.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/t4012/t4012-25e.pdf",
        mime_type=_PDF,
        source="CRA T4012 — T2 Corporation Income Tax Guide, 2025",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="Corporate filing in Canada: schedules, the small business "
        "deduction, and provincial tax including Ontario.",
    ),
    CorpusFile(
        file_id="cra-t2sch500-ontario-tax-2023",
        filename="cra-t2-schedule-500-ontario-corporation-tax-calculation-2023.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pbg/t2sch500/t2sch500-23e.pdf",
        mime_type=_PDF,
        source="CRA T2 Schedule 500 — Ontario Corporation Tax Calculation, 2023",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="How Ontario corporate tax is computed on the federal T2: "
        "basic rate, the small business deduction, and rate reductions.",
    ),
    CorpusFile(
        file_id="cra-t2sch510-ontario-minimum-tax-2014",
        filename="cra-t2-schedule-510-ontario-corporate-minimum-tax-2014.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pbg/t2sch510/t2sch510-14e.pdf",
        mime_type=_PDF,
        source="CRA T2 Schedule 510 — Ontario Corporate Minimum Tax, 2014 revision",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="Ontario's corporate minimum tax under section 55 of the "
        "Taxation Act, 2007 (Ontario): the total-assets and revenue thresholds "
        "that trigger it, and the CMT credit carry-forward.",
        notes="The 2014 revision is the current one and applies to 2009 and later "
        "tax years (stated on the schedule itself) — the year in the URL is a "
        "revision date, not staleness.",
    ),
    CorpusFile(
        file_id="cra-t4002-self-employed-2025",
        filename="cra-t4002-self-employed-business-income-guide-2025.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/t4002/t4002-25e.pdf",
        mime_type=_PDF,
        source="CRA T4002 — Self-employed Business, Professional, Commission, "
        "Farming and Fishing Income, 2025",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="Unincorporated business income: the T2125 statement, "
        "capital cost allowance classes, and deductible expenses.",
    ),
    CorpusFile(
        file_id="cra-t4068-partnership-2024",
        filename="cra-t4068-partnership-information-return-guide-2024.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/t4068/t4068-24e.pdf",
        mime_type=_PDF,
        source="CRA T4068 — Guide for the Partnership Information Return " "(T5013 forms), 2024",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="Which partnerships must file a T5013, the allocation "
        "schedules, and the late-filing penalties.",
    ),
    CorpusFile(
        file_id="cra-t4001-payroll-deductions-2025",
        filename="cra-t4001-employers-guide-payroll-deductions-2025.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/t4001/t4001-25e.pdf",
        mime_type=_PDF,
        source="CRA T4001 — Employers' Guide: Payroll Deductions and Remittances, 2025",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="CPP, EI and income tax at source: remitter types, due "
        "dates, and the penalties for late remittance.",
    ),
    CorpusFile(
        file_id="cra-t4130-taxable-benefits-2024",
        filename="cra-t4130-employers-guide-taxable-benefits-2024.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/t4130/t4130-24e.pdf",
        mime_type=_PDF,
        source="CRA T4130 — Employers' Guide: Taxable Benefits and Allowances, 2024",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="Which benefits are taxable, the automobile-benefit formulas, "
        "and GST/HST on benefits.",
    ),
    CorpusFile(
        file_id="cra-rc4110-employee-or-self-employed-2023",
        filename="cra-rc4110-employee-or-self-employed-2023.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/rc4110/rc4110-23e.pdf",
        mime_type=_PDF,
        source="CRA RC4110 — Employee or Self-employed?, 2023",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="The worker-classification tests CRA applies, and the "
        "consequences of getting the relationship wrong.",
    ),
    CorpusFile(
        file_id="cra-rc4022-gsthst-registrants-2025",
        filename="cra-rc4022-general-information-for-gst-hst-registrants-2025.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/rc4022/rc4022-25e.pdf",
        mime_type=_PDF,
        source="CRA RC4022 — General Information for GST/HST Registrants, 2025",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="Ontario's 13% HST in practice: registration thresholds, "
        "input tax credits, place-of-supply rules, and filing periods.",
    ),
    CorpusFile(
        file_id="cra-rc4058-gsthst-quick-method-2024",
        filename="cra-rc4058-quick-method-of-accounting-for-gst-hst-2024.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/rc4058/rc4058-24e.pdf",
        mime_type=_PDF,
        source="CRA RC4058 — Quick Method of Accounting for GST/HST, 2024",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="The simplified HST remittance election: eligibility, the "
        "Ontario remittance rates, and the 1% credit.",
    ),
    CorpusFile(
        file_id="cra-rc4028-gsthst-housing-rebate-2025",
        filename="cra-rc4028-gst-hst-new-housing-rebate-2025.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/rc4028/rc4028-25e.pdf",
        mime_type=_PDF,
        source="CRA RC4028 — GST/HST New Housing Rebate, 2025",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="The federal and Ontario new-housing rebates: eligibility, "
        "the price thresholds, and the claim deadlines.",
    ),
    CorpusFile(
        file_id="cra-t4037-capital-gains-2024",
        filename="cra-t4037-capital-gains-guide-2024.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/t4037/t4037-24e.pdf",
        mime_type=_PDF,
        source="CRA T4037 — Capital Gains, 2024",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="Adjusted cost base, the inclusion rate, the principal "
        "residence exemption, and the lifetime capital gains exemption.",
    ),
    CorpusFile(
        file_id="cra-t4036-rental-income-2024",
        filename="cra-t4036-rental-income-guide-2024.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/t4036/t4036-24e.pdf",
        mime_type=_PDF,
        source="CRA T4036 — Rental Income, 2024",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="Rental operations on the T776: current vs capital expenses, "
        "CCA restrictions, and co-ownership reporting.",
    ),
    CorpusFile(
        file_id="cra-t4044-employment-expenses-2024",
        filename="cra-t4044-employment-expenses-guide-2024.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/t4044/t4044-24e.pdf",
        mime_type=_PDF,
        source="CRA T4044 — Employment Expenses, 2024",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="Employee deductions on the T777, including work-space-in-"
        "the-home and the employer's T2200 certification.",
    ),
    CorpusFile(
        file_id="cra-t4013-t3-trust-guide-2024",
        filename="cra-t4013-t3-trust-guide-2024.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/t4013/t4013-24e.pdf",
        mime_type=_PDF,
        source="CRA T4013 — T3 Trust Guide, 2024",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="Trust and estate returns: the expanded reporting rules, "
        "graduated rate estates, and beneficiary allocations.",
    ),
    CorpusFile(
        file_id="cra-t4058-non-residents-2024",
        filename="cra-t4058-non-residents-and-income-tax-2024.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/t4058/t4058-24e.pdf",
        mime_type=_PDF,
        source="CRA T4058 — Non-Residents and Income Tax, 2024",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="Residency determination, Part XIII withholding, and which "
        "Canadian-source income a non-resident must report.",
    ),
    CorpusFile(
        file_id="cra-t4144-section-216-2024",
        filename="cra-t4144-electing-under-section-216-2024.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/t4144/t4144-24e.pdf",
        mime_type=_PDF,
        source="CRA T4144 — Income Tax Guide for Electing Under Section 216, 2024",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="Non-resident owners of Canadian rental property: the "
        "section 216 election, NR6, and the withholding mechanics.",
    ),
    CorpusFile(
        file_id="cra-p105-students-2024",
        filename="cra-p105-students-and-income-tax-2024.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/p105/p105-24e.pdf",
        mime_type=_PDF,
        source="CRA P105 — Students and Income Tax, 2024",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="Tuition and education amounts, scholarship exemptions, and "
        "student loan interest — a common individual filing scenario.",
    ),
    CorpusFile(
        file_id="cra-p148-resolving-dispute-2025",
        filename="cra-p148-resolving-your-dispute-2025.pdf",
        url="https://www.canada.ca/content/dam/cra-arc/formspubs/pub/p148/p148-25e.pdf",
        mime_type=_PDF,
        source="CRA P148 — Resolving Your Dispute: Objection and Appeal Rights "
        "Under the Income Tax Act, 2025",
        license="Government of Canada — non-commercial reproduction permitted",
        domain="tax",
        description="Notice of objection deadlines, the appeals process, and "
        "onward appeal to the Tax Court of Canada.",
    ),
    CorpusFile(
        file_id="justice-income-tax-act-canada",
        filename="canada-income-tax-act-consolidated.pdf",
        url="https://laws-lois.justice.gc.ca/PDF/I-3.3.pdf",
        mime_type=_PDF,
        source="Justice Laws Canada — Income Tax Act (R.S.C., 1985, c. 1 (5th "
        "Supp.)), current consolidation",
        license="Reproduction of federal law permitted (Reproduction of Federal "
        "Law Order, SI/97-5) without charge or further permission",
        domain="tax",
        description="The statute itself — the largest entry in the corpus and the "
        "ultimate authority behind every Canadian filing answer.",
        rolling=True,
        notes="The consolidation is re-published whenever the Act is amended, so "
        "its checksum is a last-seen record, not a gate.",
    ),
    CorpusFile(
        file_id="justice-excise-tax-act-canada",
        filename="canada-excise-tax-act-consolidated.pdf",
        url="https://laws-lois.justice.gc.ca/PDF/E-15.pdf",
        mime_type=_PDF,
        source="Justice Laws Canada — Excise Tax Act (R.S.C., 1985, c. E-15), "
        "current consolidation",
        license="Reproduction of federal law permitted (Reproduction of Federal "
        "Law Order, SI/97-5) without charge or further permission",
        domain="tax",
        description="GST/HST law in its statutory form — Part IX is the authority "
        "behind Ontario's 13% HST.",
        rolling=True,
    ),
    CorpusFile(
        file_id="ontario-eht-return-guide",
        filename="ontario-employer-health-tax-return-guide.pdf",
        url=(
            "https://forms.mgcs.gov.on.ca/dataset/8cbf9517-7f61-47d9-a942-421699ab8b67/"
            "resource/d7351997-3c9f-4af9-a418-d0b9f8c15464/download/2272e.pdf"
        ),
        mime_type=_PDF,
        source="Ontario Ministry of Finance — How to complete your Employer Health "
        "Tax (EHT) Return (Central Forms Repository 013-2272)",
        license="© King's Printer for Ontario — reproduction permitted for " "non-commercial use",
        domain="tax",
        description="A payroll tax Ontario administers itself: the exemption "
        "threshold, associated-employer rules, and instalment duty.",
        rolling=True,
        notes="Ontario re-publishes Central Forms Repository documents in place, "
        "so the pin is informational.",
    ),
    CorpusFile(
        file_id="ontario-land-transfer-tax-affidavit",
        filename="ontario-land-transfer-tax-affidavit.pdf",
        url=(
            "https://forms.mgcs.gov.on.ca/dataset/ffec364a-4bbb-41cc-8ea8-4692bcf937a3/"
            "resource/d19409e3-e1be-4d82-a6f3-e638597e1cae/download/0449e.pdf"
        ),
        mime_type=_PDF,
        source="Ontario Ministry of Finance — Land Transfer Tax Affidavit "
        "(Central Forms Repository 013-0449)",
        license="© King's Printer for Ontario — reproduction permitted for " "non-commercial use",
        domain="tax",
        description="Ontario land transfer tax at closing: the rate bands, the "
        "value-of-consideration statements, and the exemption codes.",
        rolling=True,
    ),
    # --- Deliberate negatives (formats outside the upload allowlist) ----------
    CorpusFile(
        file_id="census-state-population-csv",
        filename="census-state-population-dataset.csv",
        url=(
            "https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/"
            "state/totals/NST-EST2024-ALLDATA.csv"
        ),
        mime_type=_CSV,
        source="US Census Bureau — state population estimates dataset (CSV)",
        license="Public domain (US federal government work)",
        domain="statistics",
        description="Real CSV — text/csv is NOT allowlisted; ingestion must fail closed.",
        expected_ingest="rejected_type",
    ),
    CorpusFile(
        file_id="example-homepage-html",
        filename="example-homepage.html",
        url="https://example.com/",
        mime_type=_HTML,
        source="IANA — example.com reserved-domain homepage",
        license="Reserved example domain (RFC 2606); page content by IANA",
        domain="web-standards",
        description="Tiny real HTML page — text/html is NOT allowlisted for upload "
        "(web pages arrive via the web connector instead).",
        expected_ingest="rejected_type",
    ),
)


def entry_by_id(file_id: str) -> CorpusFile:
    """Return the corpus entry with ``file_id`` (raises ``KeyError`` if unknown)."""
    for entry in CORPUS:
        if entry.file_id == file_id:
            return entry
    raise KeyError(f"unknown benchmark corpus file id: {file_id}")


# --- Paths & checksums --------------------------------------------------------

_PACKAGE_DIR = Path(__file__).resolve().parent
CHECKSUMS_PATH = _PACKAGE_DIR / "checksums.json"
QUESTIONS_PATH = _PACKAGE_DIR / "questions.csv"

# Env override so CI or a dev box can keep the (large) corpus outside the repo
# checkout; the default keeps everything next to the manifest, git-ignored.
_CORPUS_DIR_ENV = "LUMEN_BENCHMARK_CORPUS_DIR"


def corpus_dir() -> Path:
    """The directory the downloaded corpus lives in (git-ignored; env-overridable)."""
    override = os.environ.get(_CORPUS_DIR_ENV)
    if override:
        return Path(override)
    return _PACKAGE_DIR / "corpus"


def extracted_dir() -> Path:
    """Cache dir for parser-extracted plain text (``extract`` writes, others read)."""
    return corpus_dir() / "_extracted"


@dataclass(frozen=True, slots=True)
class Checksum:
    """The pinned identity of one downloaded file: sha256 hex digest + byte size."""

    sha256: str
    size_bytes: int


def load_checksums(path: Path = CHECKSUMS_PATH) -> dict[str, Checksum]:
    """Load the committed pin file (``{}`` if it does not exist yet)."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    pins: dict[str, Checksum] = {}
    for file_id, value in raw.items():
        pins[file_id] = Checksum(sha256=value["sha256"], size_bytes=value["size_bytes"])
    return pins


def save_checksums(pins: dict[str, Checksum], path: Path = CHECKSUMS_PATH) -> None:
    """Write the pin file (sorted, pretty) — called by ``download --pin`` only."""
    payload = {
        file_id: {"sha256": pin.sha256, "size_bytes": pin.size_bytes}
        for file_id, pin in sorted(pins.items())
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True, slots=True)
class ManifestIssue:
    """One structural problem found in the manifest (empty list = healthy)."""

    file_id: str
    problem: str


_ALLOWLISTED_MIME_TYPES = frozenset({_PDF, _DOCX, _PPTX, _XLSX, _TXT, _MD})
# Formats we exercise; at least one entry per allowlisted type keeps the corpus
# honest about covering every parser.
_REQUIRED_MIME_COVERAGE = (_PDF, _DOCX, _PPTX, _XLSX, _TXT, _MD)


def manifest_issues(corpus: tuple[CorpusFile, ...] = CORPUS) -> list[ManifestIssue]:
    """Structural validation of the manifest itself (offline, no corpus needed)."""
    issues: list[ManifestIssue] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for entry in corpus:
        if entry.file_id in seen_ids:
            issues.append(ManifestIssue(entry.file_id, "duplicate file_id"))
        seen_ids.add(entry.file_id)
        if entry.filename in seen_names:
            issues.append(ManifestIssue(entry.file_id, "duplicate filename"))
        seen_names.add(entry.filename)
        if not entry.url.startswith("https://"):
            issues.append(ManifestIssue(entry.file_id, f"non-https url: {entry.url}"))
        allowlisted = entry.mime_type in _ALLOWLISTED_MIME_TYPES
        if entry.expected_ingest == "ok" and not allowlisted:
            issues.append(
                ManifestIssue(
                    entry.file_id,
                    f"expected_ingest=ok but mime {entry.mime_type!r} is not allowlisted",
                )
            )
        if entry.expected_ingest == "rejected_type" and allowlisted:
            issues.append(
                ManifestIssue(
                    entry.file_id,
                    f"expected_ingest=rejected_type but mime {entry.mime_type!r} is allowlisted",
                )
            )
        if entry.smoke and entry.expected_ingest != "ok":
            issues.append(ManifestIssue(entry.file_id, "smoke subset must be ingestable"))
        if entry.rolling and entry.expected_ingest != "ok":
            issues.append(ManifestIssue(entry.file_id, "a rolling entry must be ingestable"))
        if entry.rolling and entry.smoke:
            issues.append(
                ManifestIssue(
                    entry.file_id,
                    "a rolling entry cannot be in the smoke subset (smoke feeds the "
                    "pinned-grounding eval harness)",
                )
            )
    for mime in _REQUIRED_MIME_COVERAGE:
        ok_entries = [e for e in corpus if e.mime_type == mime and e.expected_ingest == "ok"]
        if not ok_entries:
            issues.append(ManifestIssue("<corpus>", f"no ingestable entry for {mime}"))
        if not any(e.smoke for e in ok_entries):
            issues.append(ManifestIssue("<corpus>", f"smoke subset misses {mime}"))
    return issues


__all__ = [
    "CHECKSUMS_PATH",
    "CORPUS",
    "QUESTIONS_PATH",
    "Checksum",
    "CorpusFile",
    "ExpectedIngest",
    "ManifestIssue",
    "SizeClass",
    "TextQuality",
    "corpus_dir",
    "entry_by_id",
    "extracted_dir",
    "load_checksums",
    "manifest_issues",
    "save_checksums",
    "size_class",
]
