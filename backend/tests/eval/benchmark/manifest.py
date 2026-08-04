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
