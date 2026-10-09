"""Immutable literal rules share the taxonomy release identity."""

from __future__ import annotations

import json
from typing import Any

from app.classification.taxonomy import DATA, _nodes, load_taxonomy


def load_rules(version: str = "1.0.0") -> dict[str, Any]:
    taxonomy = load_taxonomy(version)
    rules: dict[str, Any] = json.loads((DATA / f"rules-{version}.json").read_text("utf-8"))
    nodes = _nodes(taxonomy)
    if rules["taxonomy_version"] != version or not rules["rules_version"]:
        raise ValueError("rule release identity mismatch")
    ids: set[str] = set()
    for rule in rules["rules"]:
        if rule["id"] in ids or rule["path"] not in nodes or nodes[rule["path"]].get("children"):
            raise ValueError("ambiguous or invalid rule target")
        ids.add(rule["id"])
    return rules
