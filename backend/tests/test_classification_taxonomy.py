"""ADR-0028 / #689: hierarchy, safe unknowns and immutable ID history."""

import copy

import pytest

from app.classification.taxonomy import (
    TaxonomyError,
    load_taxonomy,
    validate_history,
    validate_taxonomy,
)


def test_published_taxonomy_covers_domains_facets_and_three_levels():
    taxonomy = load_taxonomy()
    validate_taxonomy(taxonomy)
    expected = {
        "legal",
        "financial",
        "tax",
        "regulatory",
        "environmental",
        "safety",
        "engineering",
        "academic",
        "medical",
        "government",
        "hr",
        "marketing",
        "correspondence",
        "forms",
        "data",
        "presentations",
        "other",
    }
    assert {node["id"] for node in taxonomy["nodes"]} == expected
    assert {facet["id"] for facet in taxonomy["facets"]} >= {
        "scanned",
        "contains_tables",
        "contains_personal_data",
        "draft",
        "signed",
        "multi_document_bundle",
    }
    for domain in taxonomy["nodes"]:
        assert domain["children"]
        for family in domain["children"]:
            assert family["children"]
            for leaf in family["children"]:
                assert leaf["description"] and leaf["signals"] and leaf["examples"]


@pytest.mark.parametrize("field", ["description", "examples", "signals"])
def test_missing_model_guidance_is_rejected(field):
    taxonomy = copy.deepcopy(load_taxonomy())
    taxonomy["nodes"][0]["children"][0]["children"][0][field] = [] if field != "description" else ""
    with pytest.raises(TaxonomyError):
        validate_taxonomy(taxonomy)


def test_wrong_parent_and_missing_other_are_rejected():
    taxonomy = copy.deepcopy(load_taxonomy())
    taxonomy["nodes"][0]["children"][0]["id"] = "tax/not-legal"
    with pytest.raises(TaxonomyError):
        validate_taxonomy(taxonomy)
    taxonomy = copy.deepcopy(load_taxonomy())
    taxonomy["nodes"] = [node for node in taxonomy["nodes"] if node["id"] != "other"]
    with pytest.raises(TaxonomyError):
        validate_taxonomy(taxonomy)


def test_removal_requires_explicit_valid_migration_and_ids_are_never_reused():
    old = load_taxonomy()
    new = copy.deepcopy(old)
    new["version"] = "1.1.0"
    family = new["nodes"][0]["children"][0]
    removed = family["children"].pop(0)["id"]
    with pytest.raises(TaxonomyError, match="mapping"):
        validate_history([old, new])
    new["migrations"] = [
        {"from_version": old["version"], "mapping": {removed: family["children"][-1]["id"]}}
    ]
    validate_history([old, new])
    new["migrations"][0]["mapping"][removed] = "missing/replacement"
    with pytest.raises(TaxonomyError, match="target"):
        validate_history([old, new])
    new["migrations"][0]["mapping"][removed] = None
    validate_history([old, new])
    reused = copy.deepcopy(old)
    reused["version"] = "1.2.0"
    with pytest.raises(TaxonomyError, match="reused"):
        validate_history([old, new, reused])


def test_no_duplicate_ids_unknown_fields_or_version_reuse():
    taxonomy = copy.deepcopy(load_taxonomy())
    taxonomy["nodes"].append(taxonomy["nodes"][0])
    with pytest.raises(TaxonomyError):
        validate_taxonomy(taxonomy)
    taxonomy = copy.deepcopy(load_taxonomy())
    taxonomy["made_up"] = True
    with pytest.raises(TaxonomyError):
        validate_taxonomy(taxonomy)
    with pytest.raises(TaxonomyError, match="version"):
        validate_history([load_taxonomy(), load_taxonomy()])


def test_published_release_history_is_compatible():
    validate_history()
