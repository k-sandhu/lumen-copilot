"""ADR-0028 taxonomy loader and release compatibility validation."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

DATA = Path(__file__).parent / "data"


class TaxonomyError(ValueError):
    """Invalid reference data or incompatible release history."""


def load_taxonomy(version: str = "1.0.0") -> dict[str, Any]:
    # A version is an identifier, never an arbitrary filesystem path.
    if not version or any(c not in "0123456789." for c in version):
        raise TaxonomyError("Invalid taxonomy version")
    value: dict[str, Any] = json.loads((DATA / f"taxonomy-{version}.json").read_text("utf-8"))
    validate_taxonomy(value)
    return value


def _nodes(taxonomy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    def walk(nodes: list[dict[str, Any]], parent: str, depth: int) -> None:
        if not any(node["id"] == f"{parent}other" for node in nodes):
            raise TaxonomyError(f"Missing other sibling under {parent or 'root'}")
        for node in nodes:
            identifier = node["id"]
            if identifier in result:
                raise TaxonomyError(f"Duplicate node ID: {identifier}")
            if not identifier.startswith(parent) or "/" in identifier[len(parent) :]:
                raise TaxonomyError(f"Invalid parent path: {identifier}")
            result[identifier] = node
            children = node.get("children", [])
            if depth < 3 and not children:
                raise TaxonomyError(f"Missing family/type children: {identifier}")
            if depth > 4 or (depth == 4 and children):
                raise TaxonomyError("Taxonomy exceeds optional subtype depth")
            if children:
                walk(children, identifier + "/", depth + 1)

    walk(taxonomy["nodes"], "", 1)
    return result


def validate_taxonomy(taxonomy: dict[str, Any]) -> None:
    schema = json.loads((DATA / "taxonomy.schema.json").read_text("utf-8"))
    try:
        Draft202012Validator(schema).validate(taxonomy)
    except ValidationError as exc:
        raise TaxonomyError("Taxonomy does not satisfy its JSON Schema") from exc
    _nodes(taxonomy)
    facets = [facet["id"] for facet in taxonomy["facets"]]
    if len(facets) != len(set(facets)):
        raise TaxonomyError("Duplicate facet ID")


def validate_history(releases: Iterable[dict[str, Any]] | None = None) -> None:
    """Check every immutable release; retired node/facet IDs stay tombstoned.

    Semantic meaning changes are reviewed as new IDs; text clarifications alone
    cannot be mechanically distinguished from meaning changes.
    """
    if releases is None:
        releases = [json.loads(path.read_text("utf-8")) for path in DATA.glob("taxonomy-*.json")]
    ordered = sorted(releases, key=lambda release: tuple(map(int, release["version"].split("."))))
    versions: set[str] = set()
    retired: set[str] = set()
    previous: dict[str, Any] | None = None
    previous_ids: set[str] = set()
    for release in ordered:
        validate_taxonomy(release)
        if release["version"] in versions:
            raise TaxonomyError("Published version reused")
        versions.add(release["version"])
        ids = set(_nodes(release)) | {"facet:" + facet["id"] for facet in release["facets"]}
        if retired & ids:
            raise TaxonomyError("Retired ID reused")
        migrations = release["migrations"]
        if previous is None:
            if migrations:
                raise TaxonomyError("Initial release cannot have a migration")
        else:
            applicable = [
                entry for entry in migrations if entry["from_version"] == previous["version"]
            ]
            removed = previous_ids - ids
            if len(applicable) > 1 or (removed and not applicable):
                raise TaxonomyError("Missing or ambiguous migration mapping")
            mapping = applicable[0]["mapping"] if applicable else {}
            if set(mapping) != removed:
                raise TaxonomyError("Migration mapping must cover exactly the retired IDs")
            if any(target is not None and target not in ids for target in mapping.values()):
                raise TaxonomyError("Migration target does not exist")
            if any(entry["from_version"] != previous["version"] for entry in migrations):
                raise TaxonomyError("Migration must name the preceding release")
            retired |= removed
        previous, previous_ids = release, ids
