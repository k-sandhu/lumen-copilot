"""Tenant-bound operational snapshots; domain values contain no credentials."""

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

_PATH = re.compile(r"[a-z][a-z0-9_]*(/[a-z][a-z0-9_]*){0,3}")
_FACET = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True, slots=True)
class ClassificationMetadata:
    """Content-free search projection. Unknown facets are absent, never false."""

    path: str
    taxonomy_version: str
    facets: tuple[tuple[str, bool], ...]
    confidence: float | None
    method: str

    @classmethod
    def from_result(cls, status: str, result: dict[str, Any]) -> "ClassificationMetadata | None":
        path, version = result.get("path"), result.get("taxonomy_version")
        if status not in {"classified", "override"} or not isinstance(path, str):
            return None
        if not _PATH.fullmatch(path) or len(path) > 512 or not isinstance(version, str):
            return None
        facets = tuple(
            sorted(
                (key, facet["value"])
                for key, facet in result.get("facets", {}).items()
                if _FACET.fullmatch(key)
                and isinstance(facet, dict)
                and type(facet.get("value")) is bool
            )
        )
        return cls(
            path, version, facets, result.get("confidence"), result.get("method") or "unknown"
        )

    @property
    def ancestors(self) -> tuple[str, ...]:
        parts = self.path.split("/")
        return tuple("/".join(parts[:i]) for i in range(1, len(parts) + 1))


@dataclass(frozen=True, slots=True)
class ClassificationFilter:
    taxonomy_prefix: str | None = None
    facets: tuple[tuple[str, bool], ...] = ()

    def __post_init__(self) -> None:
        if self.taxonomy_prefix is not None and (
            len(self.taxonomy_prefix) > 512 or not _PATH.fullmatch(self.taxonomy_prefix)
        ):
            raise ValueError("invalid taxonomy prefix")
        if len(self.facets) > 32 or len({key for key, _ in self.facets}) != len(self.facets):
            raise ValueError("duplicate or excessive facet predicates")
        if any(not _FACET.fullmatch(key) or type(value) is not bool for key, value in self.facets):
            raise ValueError("invalid facet predicate")

    def matches(self, metadata: ClassificationMetadata | None) -> bool:
        if metadata is None:
            return False
        return (self.taxonomy_prefix is None or self.taxonomy_prefix in metadata.ancestors) and all(
            pair in metadata.facets for pair in self.facets
        )


@dataclass(frozen=True, slots=True)
class ClassificationWork:
    tenant_id: UUID
    document_id: UUID
    input_fingerprint: str
    extraction_id: str
    taxonomy_version: str
    input_json: str
    result: dict[str, Any]
    status: str
    revision: int
    retries: int
    run_id: UUID | None
    override_path: str | None
