"""Industry pack catalog — curated, research-backed document sets (#443, #515).

Each pack gives a specialized team a realistic, licensed document set for the
knowledge work that industry actually does with AI assistants. The pack
definitions are grounded in published adoption evidence (rationales below cite
it; links in README):

* Financial services leads enterprise-AI adoption (~84%, ~47% with production
  agents) and loads **filings, shareholder letters, and regulation**.
* Healthcare is the fastest-accelerating vertical; teams load **drug labels,
  clinical guidance, and trial/monitoring procedure documents**.
* Legal/compliance teams load **statutes, privacy regulation, and contract-
  adjacent guidance** — contract/regulatory review is the highest-ROI RAG use
  case in practitioner surveys.
* Engineering orgs load **specs, vendor architecture docs, and changelogs**.
* Government/analytics teams load **statistical tables and climate reports**
  (vertical AI for healthcare/legal/government ≈ tripled to ~$3.5B in 2025).
* **Tax** is the profession's fastest-moving AI research use case: weekly use of
  AI for tax research jumped from ~33% to ~60% of practitioners in a single
  year, and tax research is the highest-uptake generative-AI task in tax and
  accounting firms. It is also the use case with the least tolerance for an
  ungrounded answer — which is why tax packs carry proven **topic coverage**
  rather than a loose pile of documents.

Packs reference immutable pinned files by id, plus (where useful) a
**rolling** entry that refreshes on demand (``load_pack --refresh``). Packs
never include the deliberate negative-format files or the pinned
poor-extraction case — a curated pack should be all signal.

**Tax-research packs prove their completeness.** A pack in the ``tax-research``
family must map every topic in :data:`TAX_TOPICS` — the aspects of tax a company
or an individual actually meets when filing — onto at least one of its own
files, and every file it carries must serve at least one topic. "Covers all
aspects of tax" is therefore a validated property (:func:`pack_issues`), not a
claim in a docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tests.eval.benchmark.manifest import CORPUS, CorpusFile, entry_by_id

# --- Tax-topic taxonomy -------------------------------------------------------

TaxTopic = Literal[
    "personal_income",
    "business_income",
    "pass_through",
    "payroll_withholding",
    "consumption_tax",
    "property_transfer",
    "credits_deductions",
    "cross_border",
    "estates_trusts",
    "filing_procedure",
    "disputes_penalties",
    "primary_authority",
    "reference_data",
]

# Canonical order (used by --list-packs and the README table) plus the one-line
# meaning of each topic. Together these define what "all aspects of tax" means
# for this corpus: the obligations a filer meets, the authority behind them, and
# the rate/threshold tables an answer has to look values up in.
TAX_TOPIC_LABELS: dict[TaxTopic, str] = {
    "personal_income": "Individual income tax return and its computation",
    "business_income": "Corporate / unincorporated business income tax",
    "pass_through": "Partnerships, S corporations and elective entity-level tax",
    "payroll_withholding": "Employer withholding, remittance and wage reporting",
    "consumption_tax": "Sales & use tax / GST-HST on supplies",
    "property_transfer": "Property tax and real-estate transfer tax",
    "credits_deductions": "Credits, deductions, depreciation and capital cost",
    "cross_border": "Non-resident, part-year and multi-jurisdiction allocation",
    "estates_trusts": "Estate tax, trusts and fiduciary returns",
    "filing_procedure": "Deadlines, instalments, elections and how to file",
    "disputes_penalties": "Audit, objection/appeal rights, penalties and interest",
    "primary_authority": "The statute, regulation or official ruling itself",
    "reference_data": "Rate schedules, threshold tables and statistics",
}

TAX_TOPICS: tuple[TaxTopic, ...] = tuple(TAX_TOPIC_LABELS)

# Terms that must actually appear in the extracted text of at least one file a
# pack maps to each topic. This is what turns the coverage claim from
# self-attestation into evidence: `pack_issues` can only prove the MAPPING is
# complete and internally consistent (every topic named, every file used, every
# id resolving) — it cannot tell whether the document behind a mapping says
# anything about the topic, because it never reads the document. A reviewer
# spotted that the validation was checking the same assertions it derived
# "coverage" from, so the semantic half is checked separately, against real
# parser output, by `tax_coverage_evidence_issues` below (corpus-dependent, so
# it runs as a test that skips when the corpus is not downloaded).
#
# Terms are lowercase and matched case-insensitively against the extracted text;
# a topic passes when ANY of its terms appears in ANY of its mapped files.
TAX_TOPIC_TERMS: dict[TaxTopic, tuple[str, ...]] = {
    "personal_income": ("income tax", "taxable income", "individual"),
    "business_income": ("corporation", "business income", "self-employ"),
    "pass_through": ("partnership", "s corporation", "flow-through", "pass-through"),
    "payroll_withholding": ("withholding", "payroll", "remit", "employer"),
    "consumption_tax": ("sales tax", "gst", "hst", "use tax"),
    "property_transfer": ("property", "land transfer", "real estate", "real property"),
    "credits_deductions": ("deduction", "credit", "depreciat", "capital cost"),
    "cross_border": ("nonresident", "non-resident", "resident of another", "alien"),
    "estates_trusts": ("estate", "trust", "beneficiar"),
    "filing_procedure": ("due date", "file", "instal", "estimated tax"),
    "disputes_penalties": ("penalt", "appeal", "objection", "interest"),
    "primary_authority": ("section", "act", "regulation", "revenue procedure", "ruling"),
    "reference_data": ("rate", "table", "threshold", "amount"),
}

# A file may legitimately serve a couple of topics (New York's IT-112-R is both
# `cross_border` and `credits_deductions`), but not many: both real packs top out
# at 2, so a file claimed for more than this is the signature of a mapping that
# was filled in rather than curated. Bounding it is what actually defeats the
# degenerate "point every topic at one comprehensive document" case — keyword
# evidence alone cannot, because a 480 KB federal tax guide genuinely does
# mention withholding, GST, trusts, penalties and rates.
_MAX_TOPICS_PER_FILE = 3

# Pack families. ``tax-research`` packs must satisfy the topic-coverage contract;
# ``industry`` packs are curated by domain and declare no coverage.
PackFamily = Literal["industry", "tax-research"]

TAX_FAMILY: PackFamily = "tax-research"


@dataclass(frozen=True, slots=True)
class TaxCoverage:
    """Which of a pack's files answer questions about one aspect of filing."""

    topic: TaxTopic
    file_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IndustryPack:
    """One curated pack: identity, the research-backed why, and its files."""

    pack_id: str
    name: str
    industry: str
    rationale: str
    file_ids: tuple[str, ...]
    family: PackFamily = "industry"
    # Only tax-research packs populate this; validation requires it of them and
    # forbids it of everyone else (no half-declared coverage).
    tax_coverage: tuple[TaxCoverage, ...] = ()


PACKS: tuple[IndustryPack, ...] = (
    IndustryPack(
        pack_id="healthcare-life-sciences",
        name="Healthcare & Life Sciences",
        industry="Pharma, providers, clinical research",
        rationale="The fastest-accelerating AI vertical: clinical teams query drug "
        "labels, dosing/interaction tables, and trial monitoring procedure "
        "documents — the exact regulated-prose shapes in this pack.",
        file_ids=(
            "fda-metformin-label",
            "fda-lipitor-label",
            "nidcr-monitoring-guidelines",
            "nidcr-monitoring-plan-template",
        ),
    ),
    IndustryPack(
        pack_id="financial-services",
        name="Financial Services",
        industry="Banking, asset management, tax",
        rationale="The highest-adoption industry (~84%): analysts load shareholder "
        "letters, regulatory capital rules, and tax instructions; the rolling "
        "current-year IRS entry demonstrates refresh-on-demand content.",
        file_ids=(
            "berkshire-2023-letter",
            "berkshire-2022-letter",
            "bis-basel3-finalisation",
            "irs-1040-instructions-2024",
            "irs-1040-instructions-current",
        ),
    ),
    IndustryPack(
        pack_id="legal-compliance",
        name="Legal & Compliance",
        industry="Law firms, privacy, policy teams",
        rationale="Contract and regulatory review is the highest-ROI RAG use case "
        "in practitioner surveys: statutes, privacy regulation (GDPR), plain-"
        "language legal guidance, and foundational legal texts.",
        file_ids=(
            "eurlex-gdpr",
            "copyright-circular-1",
            "gutenberg-us-constitution",
            "gutenberg-federalist-papers",
            "nist-sp-800-63b",
        ),
    ),
    IndustryPack(
        pack_id="software-cloud-engineering",
        name="Software & Cloud Engineering",
        industry="Platform, SRE, developer tooling",
        rationale="Engineering orgs index specs, vendor architecture guidance, and "
        "changelogs — protocol RFCs, the AWS Well-Architected Framework, an ISA "
        "reference, and real release notes.",
        file_ids=(
            "rfc9110-http-semantics",
            "rfc9112-http11",
            "ecma-404-json",
            "aws-well-architected",
            "intel-sdm-vol1",
            "kubernetes-changelog-1-31",
            "pandoc-manual",
            "fastapi-readme",
        ),
    ),
    IndustryPack(
        pack_id="government-data-climate",
        name="Government, Statistics & Climate",
        industry="Public sector, analytics, ESG",
        rationale="Vertical AI for government tripled alongside healthcare/legal: "
        "census tables, the statistical abstract, IPCC assessments, and "
        "statistical-capacity training decks.",
        file_ids=(
            "census-state-population",
            "census-county-population",
            "census-statistical-abstract-2012",
            "ipcc-ar6-wg1-spm",
            "ipcc-ar6-wg1-chapter02",
            "unsd-data-collection-tech",
            "unsd-trade-in-services",
        ),
    ),
    # --- Tax-research family -------------------------------------------------
    IndustryPack(
        pack_id="tax-research-new-york",
        name="Tax Research — New York",
        industry="Tax teams filing US federal + New York State returns",
        rationale="Weekly AI use for tax research nearly doubled to ~60% of "
        "practitioners in a year, and a New York filing question is never "
        "answerable from one jurisdiction: this pack pairs the federal layer "
        "(IRS filing guidance, an Internal Revenue Bulletin, the annual "
        "inflation revenue procedure) with the New York layer (resident and "
        "nonresident returns, franchise tax, sales tax, employer withholding, "
        "MCTMT, estate and transfer tax, and the Department's own PTET technical "
        "memorandum) so an answer can cite both.",
        family=TAX_FAMILY,
        file_ids=(
            # Federal — individual
            "irs-pub17-individual-2024",
            "irs-1040-instructions-2024",
            "irs-1040-instructions-current",
            # Federal — business & pass-through
            "irs-i1120-corporation-2024",
            "irs-i1040sc-schedule-c-2024",
            "irs-i1065-partnership-2024",
            "irs-i1120s-s-corporation-2024",
            # Federal — payroll
            "irs-pub15-circular-e-2024",
            "irs-iw2w3-wage-statements-2024",
            # Federal — deductions, procedure, disputes
            "irs-pub946-depreciation-2024",
            "irs-pub505-estimated-tax-2024",
            "irs-pub556-appeals-current",
            "irs-pub1-taxpayer-rights-current",
            # Federal — cross-border, estates, authority, data
            "irs-pub519-aliens-2024",
            "irs-i1041-estates-trusts-2024",
            "irs-irb-2025-01",
            "irs-rev-proc-2024-40",
            "irs-soi-new-york-2022",
            # New York — income tax
            "nys-it201i-resident-2024",
            "nys-it203i-nonresident-2024",
            "nys-it112ri-resident-credit-2024",
            "nys-it225i-modifications-2024",
            "nys-it2105i-estimated-tax-2024",
            # New York — entities
            "nys-ct3i-franchise-2024",
            "nys-ct3si-s-corporation-2024",
            "nys-it204i-partnership-2024",
            "nys-tsbm-ptet-2021",
            # New York — sales tax
            "nys-pub750-sales-tax-current",
            "nys-pub718-sales-tax-rates-current",
            "nys-st100i-sales-return-current",
            # New York — employment taxes
            "nys-nys45i-employer-quarterly-current",
            "nys-nys50t-withholding-tables-current",
            "nys-mta305i-mctmt-current",
            # New York — estate & property
            "nys-et706i-estate-tax-current",
            "nys-tp584i-transfer-tax-current",
            "nys-pub1093-veterans-exemption-current",
        ),
        tax_coverage=(
            TaxCoverage(
                "personal_income",
                (
                    "irs-pub17-individual-2024",
                    "irs-1040-instructions-2024",
                    "irs-1040-instructions-current",
                    "nys-it201i-resident-2024",
                    "nys-it203i-nonresident-2024",
                ),
            ),
            TaxCoverage(
                "business_income",
                (
                    "irs-i1120-corporation-2024",
                    "irs-i1040sc-schedule-c-2024",
                    "nys-ct3i-franchise-2024",
                    "nys-ct3si-s-corporation-2024",
                ),
            ),
            TaxCoverage(
                "pass_through",
                (
                    "irs-i1065-partnership-2024",
                    "irs-i1120s-s-corporation-2024",
                    "nys-it204i-partnership-2024",
                    "nys-tsbm-ptet-2021",
                ),
            ),
            TaxCoverage(
                "payroll_withholding",
                (
                    "irs-pub15-circular-e-2024",
                    "irs-iw2w3-wage-statements-2024",
                    "nys-nys45i-employer-quarterly-current",
                    "nys-nys50t-withholding-tables-current",
                    "nys-mta305i-mctmt-current",
                ),
            ),
            TaxCoverage(
                "consumption_tax",
                (
                    "nys-pub750-sales-tax-current",
                    "nys-pub718-sales-tax-rates-current",
                    "nys-st100i-sales-return-current",
                ),
            ),
            TaxCoverage(
                "property_transfer",
                (
                    "nys-tp584i-transfer-tax-current",
                    "nys-pub1093-veterans-exemption-current",
                ),
            ),
            TaxCoverage(
                "credits_deductions",
                (
                    "irs-pub946-depreciation-2024",
                    "nys-it225i-modifications-2024",
                    "nys-it112ri-resident-credit-2024",
                ),
            ),
            TaxCoverage(
                "cross_border",
                (
                    "irs-pub519-aliens-2024",
                    "nys-it203i-nonresident-2024",
                    "nys-it112ri-resident-credit-2024",
                ),
            ),
            TaxCoverage(
                "estates_trusts",
                (
                    "irs-i1041-estates-trusts-2024",
                    "nys-et706i-estate-tax-current",
                ),
            ),
            TaxCoverage(
                "filing_procedure",
                (
                    "irs-1040-instructions-2024",
                    "irs-pub505-estimated-tax-2024",
                    "nys-it2105i-estimated-tax-2024",
                    "nys-st100i-sales-return-current",
                    "nys-nys45i-employer-quarterly-current",
                ),
            ),
            TaxCoverage(
                "disputes_penalties",
                (
                    "irs-pub556-appeals-current",
                    "irs-pub1-taxpayer-rights-current",
                ),
            ),
            TaxCoverage(
                "primary_authority",
                (
                    "irs-irb-2025-01",
                    "irs-rev-proc-2024-40",
                    "nys-tsbm-ptet-2021",
                ),
            ),
            TaxCoverage(
                "reference_data",
                (
                    "irs-rev-proc-2024-40",
                    "irs-soi-new-york-2022",
                    "nys-pub718-sales-tax-rates-current",
                    "nys-nys50t-withholding-tables-current",
                ),
            ),
        ),
    ),
    IndustryPack(
        pack_id="tax-research-ontario",
        name="Tax Research — Ontario",
        industry="Tax teams filing Canadian federal + Ontario returns",
        rationale="The Canadian mirror of the New York pack: Ontario's personal and "
        "corporate income tax is computed on the federal base and administered by "
        "the CRA, so the CRA guides *are* the Ontario authority — paired here with "
        "Ontario's own ON428 and Schedule 500/510 calculations, the two taxes "
        "Ontario administers itself (Employer Health Tax and land transfer tax), "
        "and the consolidated Income Tax Act and Excise Tax Act as the statutory "
        "authority behind every answer.",
        family=TAX_FAMILY,
        file_ids=(
            # Federal + Ontario — individual
            "cra-5000-g-federal-guide-2025",
            "cra-5006-pc-ontario-guide-2025",
            "cra-5006-c-on428-2025",
            "cra-p105-students-2024",
            # Business & Ontario corporate
            "cra-t4012-t2-corporation-2025",
            "cra-t4002-self-employed-2025",
            "cra-t2sch500-ontario-tax-2023",
            "cra-t2sch510-ontario-minimum-tax-2014",
            "cra-t4068-partnership-2024",
            # Payroll
            "cra-t4001-payroll-deductions-2025",
            "cra-t4130-taxable-benefits-2024",
            "cra-rc4110-employee-or-self-employed-2023",
            "ontario-eht-return-guide",
            # GST/HST
            "cra-rc4022-gsthst-registrants-2025",
            "cra-rc4058-gsthst-quick-method-2024",
            "cra-rc4028-gsthst-housing-rebate-2025",
            # Property & capital
            "cra-t4036-rental-income-2024",
            "cra-t4037-capital-gains-2024",
            "cra-t4044-employment-expenses-2024",
            "ontario-land-transfer-tax-affidavit",
            # Cross-border, trusts, disputes
            "cra-t4058-non-residents-2024",
            "cra-t4144-section-216-2024",
            "cra-t4013-t3-trust-guide-2024",
            "cra-p148-resolving-dispute-2025",
            # Primary authority
            "justice-income-tax-act-canada",
            "justice-excise-tax-act-canada",
        ),
        tax_coverage=(
            TaxCoverage(
                "personal_income",
                (
                    "cra-5000-g-federal-guide-2025",
                    "cra-5006-pc-ontario-guide-2025",
                    "cra-5006-c-on428-2025",
                    "cra-p105-students-2024",
                ),
            ),
            TaxCoverage(
                "business_income",
                (
                    "cra-t4012-t2-corporation-2025",
                    "cra-t4002-self-employed-2025",
                    "cra-t2sch500-ontario-tax-2023",
                    "cra-t2sch510-ontario-minimum-tax-2014",
                ),
            ),
            TaxCoverage("pass_through", ("cra-t4068-partnership-2024",)),
            TaxCoverage(
                "payroll_withholding",
                (
                    "cra-t4001-payroll-deductions-2025",
                    "cra-t4130-taxable-benefits-2024",
                    "cra-rc4110-employee-or-self-employed-2023",
                    "ontario-eht-return-guide",
                ),
            ),
            TaxCoverage(
                "consumption_tax",
                (
                    "cra-rc4022-gsthst-registrants-2025",
                    "cra-rc4058-gsthst-quick-method-2024",
                    "cra-rc4028-gsthst-housing-rebate-2025",
                    "justice-excise-tax-act-canada",
                ),
            ),
            TaxCoverage(
                "property_transfer",
                (
                    "ontario-land-transfer-tax-affidavit",
                    "cra-t4036-rental-income-2024",
                    "cra-rc4028-gsthst-housing-rebate-2025",
                ),
            ),
            TaxCoverage(
                "credits_deductions",
                (
                    "cra-t4037-capital-gains-2024",
                    "cra-t4044-employment-expenses-2024",
                    "cra-t4002-self-employed-2025",
                ),
            ),
            TaxCoverage(
                "cross_border",
                (
                    "cra-t4058-non-residents-2024",
                    "cra-t4144-section-216-2024",
                ),
            ),
            TaxCoverage("estates_trusts", ("cra-t4013-t3-trust-guide-2024",)),
            TaxCoverage(
                "filing_procedure",
                (
                    "cra-5000-g-federal-guide-2025",
                    "cra-t4001-payroll-deductions-2025",
                    "cra-t4068-partnership-2024",
                ),
            ),
            TaxCoverage("disputes_penalties", ("cra-p148-resolving-dispute-2025",)),
            TaxCoverage(
                "primary_authority",
                (
                    "justice-income-tax-act-canada",
                    "justice-excise-tax-act-canada",
                ),
            ),
            TaxCoverage(
                "reference_data",
                (
                    "cra-5006-c-on428-2025",
                    "cra-t2sch500-ontario-tax-2023",
                ),
            ),
        ),
    ),
)


def pack_by_id(pack_id: str) -> IndustryPack:
    """Return the pack with ``pack_id`` (raises ``KeyError`` if unknown)."""
    for pack in PACKS:
        if pack.pack_id == pack_id:
            return pack
    known = ", ".join(p.pack_id for p in PACKS)
    raise KeyError(f"unknown pack id: {pack_id} (choose from {known})")


def pack_files(pack: IndustryPack) -> tuple[CorpusFile, ...]:
    """The pack's manifest entries, in pack order (raises on a stale id)."""
    return tuple(entry_by_id(fid) for fid in pack.file_ids)


def tax_packs(packs: tuple[IndustryPack, ...] = PACKS) -> tuple[IndustryPack, ...]:
    """Just the tax-research packs, in catalog order."""
    return tuple(p for p in packs if p.family == TAX_FAMILY)


def topics_of(pack: IndustryPack, file_id: str) -> tuple[TaxTopic, ...]:
    """Which tax topics ``file_id`` serves in ``pack`` (empty for non-tax packs)."""
    return tuple(c.topic for c in pack.tax_coverage if file_id in c.file_ids)


def pack_files_for_topic(pack: IndustryPack, topic: str) -> tuple[CorpusFile, ...]:
    """The pack's files covering ``topic``, in curated pack order.

    Raises ``KeyError`` for a topic the pack does not declare — a tax pack covers
    every topic in :data:`TAX_TOPICS`, so this only fires on a typo or on a pack
    outside the tax family.
    """
    for coverage in pack.tax_coverage:
        if coverage.topic == topic:
            wanted = set(coverage.file_ids)
            return tuple(entry_by_id(fid) for fid in pack.file_ids if fid in wanted)
    known = ", ".join(c.topic for c in pack.tax_coverage) or "<none>"
    raise KeyError(f"pack {pack.pack_id!r} declares no topic {topic!r} (has: {known})")


@dataclass(frozen=True, slots=True)
class PackIssue:
    """One structural problem in the pack catalog (empty list = healthy)."""

    pack_id: str
    problem: str


def _tax_coverage_issues(pack: IndustryPack) -> list[PackIssue]:
    """Validate the topic-coverage contract for one tax-research pack."""
    issues: list[PackIssue] = []
    own_files = set(pack.file_ids)
    seen_topics: set[str] = set()
    covered_files: set[str] = set()
    for coverage in pack.tax_coverage:
        if coverage.topic not in TAX_TOPIC_LABELS:
            issues.append(PackIssue(pack.pack_id, f"unknown tax topic {coverage.topic!r}"))
            continue
        if coverage.topic in seen_topics:
            issues.append(PackIssue(pack.pack_id, f"duplicate topic {coverage.topic!r}"))
        seen_topics.add(coverage.topic)
        if not coverage.file_ids:
            issues.append(PackIssue(pack.pack_id, f"topic {coverage.topic!r} has no files"))
        if len(set(coverage.file_ids)) != len(coverage.file_ids):
            issues.append(PackIssue(pack.pack_id, f"topic {coverage.topic!r} repeats a file id"))
        for fid in coverage.file_ids:
            if fid not in own_files:
                issues.append(
                    PackIssue(
                        pack.pack_id,
                        f"topic {coverage.topic!r} cites {fid!r}, which is not in the pack",
                    )
                )
            covered_files.add(fid)
    for topic in TAX_TOPICS:
        if topic not in seen_topics:
            issues.append(
                PackIssue(
                    pack.pack_id,
                    f"tax pack does not cover {topic!r} ({TAX_TOPIC_LABELS[topic]})",
                )
            )
    # Every file must earn its place: an uncovered file is either mis-curated or
    # a missing coverage entry, and both are silent gaps without this check.
    for fid in pack.file_ids:
        if fid not in covered_files:
            issues.append(PackIssue(pack.pack_id, f"{fid!r} serves no declared tax topic"))

    # --- non-degeneracy -----------------------------------------------------
    # The checks above prove the mapping is COMPLETE and CONSISTENT. They cannot
    # prove a document substantively covers a topic — nothing here reads the
    # document. What they can do is reject a mapping that is complete on paper
    # but vacuous in fact, which is the realistic failure: reusing one broad
    # document for everything.
    per_file: dict[str, int] = {}
    for coverage in pack.tax_coverage:
        for fid in coverage.file_ids:
            per_file[fid] = per_file.get(fid, 0) + 1
    for fid, count in sorted(per_file.items()):
        if count > _MAX_TOPICS_PER_FILE:
            issues.append(
                PackIssue(
                    pack.pack_id,
                    f"{fid!r} is claimed for {count} topics (max {_MAX_TOPICS_PER_FILE}) — "
                    "one document standing in for most of the taxonomy is not coverage",
                )
            )
    seen_sets: dict[frozenset[str], str] = {}
    for coverage in pack.tax_coverage:
        key = frozenset(coverage.file_ids)
        if key in seen_sets:
            issues.append(
                PackIssue(
                    pack.pack_id,
                    f"topics {seen_sets[key]!r} and {coverage.topic!r} map to an identical "
                    "file set — two aspects of filing backed by exactly the same documents "
                    "means at least one is not really covered",
                )
            )
        else:
            seen_sets[key] = coverage.topic
    return issues


def pack_issues(
    packs: tuple[IndustryPack, ...] = PACKS,
    corpus: tuple[CorpusFile, ...] = CORPUS,
) -> list[PackIssue]:
    """Validate the catalog: ids resolve, packs are all-signal, no duplicates.

    Tax-research packs additionally have to prove they cover every topic in
    :data:`TAX_TOPICS` using only their own files (:func:`_tax_coverage_issues`).
    """
    issues: list[PackIssue] = []
    by_id = {e.file_id: e for e in corpus}
    seen_packs: set[str] = set()
    for pack in packs:
        if pack.pack_id in seen_packs:
            issues.append(PackIssue(pack.pack_id, "duplicate pack_id"))
        seen_packs.add(pack.pack_id)
        if len(pack.file_ids) < 4:
            issues.append(PackIssue(pack.pack_id, "a pack needs at least 4 files"))
        if len(set(pack.file_ids)) != len(pack.file_ids):
            issues.append(PackIssue(pack.pack_id, "duplicate file ids within the pack"))
        for fid in pack.file_ids:
            entry = by_id.get(fid)
            if entry is None:
                issues.append(PackIssue(pack.pack_id, f"unknown file id {fid!r}"))
                continue
            if entry.expected_ingest != "ok":
                issues.append(
                    PackIssue(
                        pack.pack_id, f"{fid!r} is a negative-format file — not pack material"
                    )
                )
            if entry.text_quality != "good":
                issues.append(
                    PackIssue(
                        pack.pack_id,
                        f"{fid!r} is a poor-extraction file — a curated pack must be all signal",
                    )
                )
        if pack.family == TAX_FAMILY:
            if not pack.tax_coverage:
                issues.append(
                    PackIssue(pack.pack_id, "tax-research pack declares no topic coverage")
                )
            else:
                issues.extend(_tax_coverage_issues(pack))
        elif pack.tax_coverage:
            issues.append(
                PackIssue(
                    pack.pack_id,
                    f"only {TAX_FAMILY!r} packs may declare tax coverage "
                    f"(family={pack.family!r})",
                )
            )
    return issues


def tax_coverage_evidence_issues(pack: IndustryPack, extracted: dict[str, str]) -> list[PackIssue]:
    """Check each topic's mapping against the REAL extracted text (ADR-0021).

    ``pack_issues`` proves the coverage mapping is structurally complete; this
    proves it is not fiction. For every topic, at least one of the files the
    pack maps to it must contain one of :data:`TAX_TOPIC_TERMS` for that topic
    in the text the ingestion parsers actually produce. Mapping every topic to
    the same arbitrary document — the concrete way a reviewer showed the
    structural check could be satisfied while the claim was false — fails here.

    ``extracted`` maps ``file_id -> extracted text``; files absent from it are
    skipped (the caller decides how much of the corpus is downloaded), and a
    topic whose files are ALL absent is reported as unverified rather than
    silently passing.
    """
    issues: list[PackIssue] = []
    for coverage in pack.tax_coverage:
        terms = TAX_TOPIC_TERMS.get(coverage.topic, ())
        if not terms:
            continue
        available = [f for f in coverage.file_ids if f in extracted]
        if not available:
            issues.append(
                PackIssue(pack.pack_id, f"topic {coverage.topic!r}: no mapped file extracted")
            )
            continue
        if not any(
            term in extracted[file_id].casefold() for file_id in available for term in terms
        ):
            issues.append(
                PackIssue(
                    pack.pack_id,
                    f"topic {coverage.topic!r} is not evidenced by any of its files "
                    f"{available!r} — none mentions any of {list(terms)!r}",
                )
            )
    return issues


__all__ = [
    "PACKS",
    "TAX_FAMILY",
    "TAX_TOPICS",
    "TAX_TOPIC_TERMS",
    "TAX_TOPIC_LABELS",
    "IndustryPack",
    "PackFamily",
    "PackIssue",
    "TaxCoverage",
    "TaxTopic",
    "pack_by_id",
    "pack_files",
    "pack_files_for_topic",
    "pack_issues",
    "tax_coverage_evidence_issues",
    "tax_packs",
    "topics_of",
]
