"""Industry pack catalog — curated, research-backed document sets (#443).

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

Packs reference immutable pinned files by id, plus (where useful) a
**rolling** entry that refreshes on demand (``load_pack --refresh``). Packs
never include the deliberate negative-format files or the pinned
poor-extraction case — a curated pack should be all signal.
"""

from __future__ import annotations

from dataclasses import dataclass

from tests.eval.benchmark.manifest import CORPUS, CorpusFile, entry_by_id


@dataclass(frozen=True, slots=True)
class IndustryPack:
    """One curated pack: identity, the research-backed why, and its files."""

    pack_id: str
    name: str
    industry: str
    rationale: str
    file_ids: tuple[str, ...]


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


@dataclass(frozen=True, slots=True)
class PackIssue:
    """One structural problem in the pack catalog (empty list = healthy)."""

    pack_id: str
    problem: str


def pack_issues(
    packs: tuple[IndustryPack, ...] = PACKS,
    corpus: tuple[CorpusFile, ...] = CORPUS,
) -> list[PackIssue]:
    """Validate the catalog: ids resolve, packs are all-signal, no duplicates."""
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
    return issues


__all__ = ["PACKS", "IndustryPack", "PackIssue", "pack_by_id", "pack_files", "pack_issues"]
