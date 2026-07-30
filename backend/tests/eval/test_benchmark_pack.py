"""Data-pack loader guarantees (#441) — deterministic selection + resilient client.

The selection rule is the loader's contract: same inputs ⇒ same files, balanced
across the chosen formats, never a negative-format entry. The client tests pin
the 401-refresh replay that long ingestion waits depend on.
"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from tests.eval.benchmark.bank import (
    BenchmarkQuestion,
    Evidence,
    bank_issues,
    load_questions,
)
from tests.eval.benchmark.client import ApiClient
from tests.eval.benchmark.load_pack import FORMAT_MIME, select_from_pack, select_pack
from tests.eval.benchmark.manifest import CORPUS, manifest_issues
from tests.eval.benchmark.packs import (
    PACKS,
    TAX_TOPICS,
    IndustryPack,
    TaxCoverage,
    pack_by_id,
    pack_files,
    pack_files_for_topic,
    pack_issues,
    tax_packs,
    topics_of,
)

# --- Deterministic selection ---------------------------------------------------


def test_selection_is_deterministic() -> None:
    """Same flags twice ⇒ byte-identical selection."""
    first = select_pack(["pdf", "xlsx"], 5)
    second = select_pack(["pdf", "xlsx"], 5)
    assert [e.file_id for e in first] == [e.file_id for e in second]
    assert len(first) == 5


def test_round_robin_balances_formats() -> None:
    """count=6 over all formats ⇒ exactly one file of each of the six formats."""
    selection = select_pack(None, 6)
    mimes = [e.mime_type for e in selection]
    assert len(set(mimes)) == 6, f"expected one per format, got {mimes}"


def test_round_robin_follows_caller_format_order() -> None:
    """--formats xlsx,pdf --count 1 ⇒ an XLSX (the caller's first format)."""
    selection = select_pack(["xlsx", "pdf"], 1)
    assert selection[0].mime_type == FORMAT_MIME["xlsx"]


def test_single_format_takes_manifest_order() -> None:
    """A one-format selection is the first N of that format in manifest order."""
    expected = [
        e.file_id for e in CORPUS if e.expected_ingest == "ok" and e.mime_type == FORMAT_MIME["pdf"]
    ][:3]
    selection = select_pack(["pdf"], 3)
    assert [e.file_id for e in selection] == expected


def test_negative_format_entries_are_never_selected() -> None:
    """Even selecting everything, the deliberate CSV/HTML negatives stay out."""
    selection = select_pack(None, None)
    assert all(e.expected_ingest == "ok" for e in selection)
    eligible = sum(1 for e in CORPUS if e.expected_ingest == "ok")
    assert len(selection) == eligible


def test_count_beyond_eligible_caps_at_eligible() -> None:
    selection = select_pack(["pptx"], 99)
    eligible = sum(
        1 for e in CORPUS if e.expected_ingest == "ok" and e.mime_type == FORMAT_MIME["pptx"]
    )
    assert len(selection) == eligible


def test_unknown_format_and_bad_count_raise() -> None:
    """Misuse fails loudly — never a silent empty selection (INV-8 flavour)."""
    with pytest.raises(ValueError, match="unknown format"):
        select_pack(["csv"], 1)  # the negative format is not offerable
    with pytest.raises(ValueError, match="positive"):
        select_pack(["pdf"], 0)
    with pytest.raises(ValueError, match="at least one"):
        select_pack([" "], 1)


def test_duplicate_formats_collapse() -> None:
    a = select_pack(["pdf", "pdf", "PDF"], 2)
    b = select_pack(["pdf"], 2)
    assert [e.file_id for e in a] == [e.file_id for e in b]


# --- Industry pack catalog (#443) ----------------------------------------------


def test_pack_catalog_is_structurally_sound() -> None:
    """Ids resolve, packs are all-signal (no negatives / poor extraction), ≥4 files."""
    issues = pack_issues()
    assert issues == [], "\n".join(f"{i.pack_id}: {i.problem}" for i in issues)
    assert len(PACKS) == 7  # 5 industry + 2 tax-research


def test_pack_selection_is_deterministic_and_ordered() -> None:
    """--pack keeps the curated order; twice ⇒ identical."""
    first = select_from_pack("financial-services")
    second = select_from_pack("financial-services")
    assert [e.file_id for e in first] == [e.file_id for e in second]
    assert first[0].file_id == "berkshire-2023-letter"


def test_pack_format_and_count_filters_compose() -> None:
    """--pack legal-compliance --formats txt --count 1 ⇒ the Constitution."""
    selection = select_from_pack("legal-compliance", ["txt"], 1)
    assert [e.file_id for e in selection] == ["gutenberg-us-constitution"]


def test_unknown_pack_raises() -> None:
    with pytest.raises(KeyError, match="unknown pack"):
        select_from_pack("aerospace")


def test_rolling_entries_are_marked_and_outside_smoke() -> None:
    """The rolling contract: ingestable, never in smoke, never cited by questions."""
    rolling = [e for e in CORPUS if e.rolling]
    assert rolling, "the catalog should keep a rolling refresh-on-demand example"
    for entry in rolling:
        assert entry.expected_ingest == "ok"
        assert not entry.smoke
    assert manifest_issues() == []


def test_questions_never_cite_rolling_files() -> None:
    """A question grounded in a rolling file would break on refresh — forbidden."""
    rolling_ids = {e.file_id for e in CORPUS if e.rolling}
    for q in load_questions():
        for ev in q.evidence:
            assert ev.file_id not in rolling_ids, f"{q.qid} cites rolling {ev.file_id}"


def test_bank_validation_rejects_a_rolling_citation() -> None:
    """The negative: bank_issues flags a question that cites a rolling file."""
    rolling_id = next(e.file_id for e in CORPUS if e.rolling)
    bad = BenchmarkQuestion(
        qid="bm-999",
        question="What does the current-year instruction booklet say?",
        category="single_hop",
        difficulty="easy",
        answerable=True,
        gold_answer="x",
        answer_facts=("a fact that appears in the quote below",),
        source_files=(rolling_id,),
        evidence=(
            Evidence(
                file_id=rolling_id,
                locator="p. 1",
                quote="a fact that appears in the quote below, padded for length",
            ),
        ),
    )
    problems = bank_issues((bad,))
    assert any("rolling" in i.problem for i in problems)


# --- Tax-research packs (#515) --------------------------------------------------
#
# A tax pack's product promise is *coverage*: every aspect of tax a company or an
# individual meets when filing has at least one document behind it. These tests
# are that promise's mechanism — they fail if a topic loses its last file, if a
# file stops serving any topic, or if a pack claims coverage it cannot back.

TAX_PACK_IDS = ("tax-research-new-york", "tax-research-ontario")


def test_tax_packs_are_registered() -> None:
    assert [p.pack_id for p in tax_packs()] == list(TAX_PACK_IDS)


@pytest.mark.parametrize("pack_id", TAX_PACK_IDS)
def test_tax_pack_covers_every_aspect_of_filing(pack_id: str) -> None:
    """The headline guarantee: no topic in the taxonomy is left without a document."""
    pack = pack_by_id(pack_id)
    covered = {c.topic for c in pack.tax_coverage}
    assert covered == set(TAX_TOPICS), f"missing: {sorted(set(TAX_TOPICS) - covered)}"
    for coverage in pack.tax_coverage:
        assert coverage.file_ids, f"{coverage.topic} has no files"


@pytest.mark.parametrize("pack_id", TAX_PACK_IDS)
def test_every_tax_pack_file_serves_a_topic(pack_id: str) -> None:
    """No filler: each file is claimed by at least one aspect of filing."""
    pack = pack_by_id(pack_id)
    orphans = [fid for fid in pack.file_ids if not topics_of(pack, fid)]
    assert orphans == [], f"files serving no topic: {orphans}"


@pytest.mark.parametrize("pack_id", TAX_PACK_IDS)
def test_tax_pack_files_are_tax_domain_and_licensed(pack_id: str) -> None:
    """Every entry is a real tax document with recorded provenance and licence."""
    for entry in pack_files(pack_by_id(pack_id)):
        assert entry.domain == "tax", f"{entry.file_id} is domain={entry.domain}"
        assert entry.source and entry.license
        assert entry.url.startswith("https://")


def test_tax_packs_span_both_jurisdictional_layers() -> None:
    """A filing answer needs federal *and* sub-national sources — both are present."""
    ny = pack_by_id("tax-research-new-york").file_ids
    assert any(f.startswith("irs-") for f in ny), "no US federal layer"
    assert any(f.startswith("nys-") for f in ny), "no New York State layer"

    on = pack_by_id("tax-research-ontario").file_ids
    assert any(f.startswith("cra-") for f in on), "no Canadian federal layer"
    assert any(f.startswith("ontario-") for f in on), "no Ontario-administered layer"


def test_tax_packs_carry_statutory_or_official_authority() -> None:
    """primary_authority must resolve to real statutes/rulings, not more guidance."""
    for pack_id in TAX_PACK_IDS:
        pack = pack_by_id(pack_id)
        files = pack_files_for_topic(pack, "primary_authority")
        assert files, f"{pack_id} has no primary authority"


# --- Topic-scoped selection ----------------------------------------------------


def test_topic_selection_is_deterministic_and_pack_ordered() -> None:
    """--tax-topic keeps curated pack order and is stable across calls."""
    first = select_from_pack("tax-research-ontario", None, None, "payroll_withholding")
    second = select_from_pack("tax-research-ontario", None, None, "payroll_withholding")
    assert [e.file_id for e in first] == [e.file_id for e in second]
    pack = pack_by_id("tax-research-ontario")
    order = [f for f in pack.file_ids if f in {e.file_id for e in first}]
    assert [e.file_id for e in first] == order


def test_topic_selection_narrows_to_the_topic() -> None:
    selection = select_from_pack("tax-research-new-york", None, None, "consumption_tax")
    pack = pack_by_id("tax-research-new-york")
    for entry in selection:
        assert "consumption_tax" in topics_of(pack, entry.file_id)


def test_topic_composes_with_formats_and_count() -> None:
    selection = select_from_pack("tax-research-new-york", ["pdf"], 2, "payroll_withholding")
    assert len(selection) == 2
    assert all(e.mime_type == FORMAT_MIME["pdf"] for e in selection)


def test_unknown_tax_topic_raises() -> None:
    with pytest.raises(KeyError, match="declares no topic"):
        select_from_pack("tax-research-ontario", None, None, "carbon_tax")


def test_topic_on_a_non_tax_pack_raises() -> None:
    """An industry pack declares no coverage, so any topic is a hard error."""
    with pytest.raises(KeyError, match="declares no topic"):
        select_from_pack("financial-services", None, None, "personal_income")


def test_empty_pack_selection_raises_rather_than_loading_nothing() -> None:
    """The Ontario pack is all PDF — asking for XLSX must fail, not no-op."""
    with pytest.raises(ValueError, match="no files in pack"):
        select_from_pack("tax-research-ontario", ["xlsx"])


# --- Coverage-contract negatives ------------------------------------------------


def _mutated(pack_id: str, **changes: object) -> tuple[IndustryPack, ...]:
    """The real pack with one field replaced — the input to a negative check."""
    return (replace(pack_by_id(pack_id), **changes),)  # type: ignore[arg-type]


def test_tax_pack_missing_a_topic_is_rejected() -> None:
    pack = pack_by_id("tax-research-ontario")
    dropped = tuple(c for c in pack.tax_coverage if c.topic != "estates_trusts")
    issues = pack_issues(_mutated(pack.pack_id, tax_coverage=dropped))
    assert any("does not cover 'estates_trusts'" in i.problem for i in issues)


def test_tax_pack_with_no_coverage_is_rejected() -> None:
    issues = pack_issues(_mutated("tax-research-ontario", tax_coverage=()))
    assert any("declares no topic coverage" in i.problem for i in issues)


def test_coverage_citing_a_file_outside_the_pack_is_rejected() -> None:
    pack = pack_by_id("tax-research-ontario")
    smuggled = (
        TaxCoverage("estates_trusts", ("gutenberg-moby-dick",)),
        *(c for c in pack.tax_coverage if c.topic != "estates_trusts"),
    )
    issues = pack_issues(_mutated(pack.pack_id, tax_coverage=smuggled))
    assert any("not in the pack" in i.problem for i in issues)


def test_pack_file_serving_no_topic_is_rejected() -> None:
    """Adding a file without claiming a topic for it is a silent gap — caught."""
    pack = pack_by_id("tax-research-ontario")
    issues = pack_issues(_mutated(pack.pack_id, file_ids=(*pack.file_ids, "gutenberg-moby-dick")))
    assert any("serves no declared tax topic" in i.problem for i in issues)


def test_non_tax_pack_may_not_declare_tax_coverage() -> None:
    """Half-declared coverage is worse than none — an industry pack must not have it."""
    issues = pack_issues(
        _mutated("financial-services", tax_coverage=(TaxCoverage("personal_income", ()),))
    )
    assert any("may declare tax coverage" in i.problem for i in issues)


def test_unknown_topic_in_coverage_is_rejected() -> None:
    pack = pack_by_id("tax-research-ontario")
    bogus = (TaxCoverage("carbon_tax", pack.file_ids[:1]), *pack.tax_coverage)  # type: ignore[arg-type]
    issues = pack_issues(_mutated(pack.pack_id, tax_coverage=bogus))
    assert any("unknown tax topic" in i.problem for i in issues)


# --- ApiClient 401-refresh replay ----------------------------------------------


def test_client_relogs_in_once_on_401() -> None:
    """An expired token mid-run triggers exactly one re-login and a replay."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(200, json={"access_token": f"tok{len(calls)}"})
        if request.headers.get("Authorization") == "Bearer tok1":
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, json={"items": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        api = ApiClient(http, "http://stack", "u@x.test", "pw")
        api.login()  # -> tok1
        response = api.request("GET", "/api/v1/documents")

    assert response.status_code == 200
    assert calls == [
        "POST /api/v1/auth/login",
        "GET /api/v1/documents",  # 401 with the stale token
        "POST /api/v1/auth/login",  # transparent re-login
        "GET /api/v1/documents",  # replay succeeds
    ]
