"""The engine-side permission filter — the INV-1/INV-2 predicate as an OpenSearch filter.

This module is the pure half of the ``app/search/`` boundary (ADR-0010 §4): it
translates a caller's resolved allow-set into the ``bool`` filter every engine
query MUST carry. It is deliberately shaped like
:class:`app.retrieval.permissions.AllowSet` — a small value object with **no
constructor that can yield an unfiltered query**:

* ``tenant_id`` is a required field (INV-1) — a filter without it cannot exist;
* ``owner_ids`` must be non-empty (the requester always allows their own
  resources), mirroring ``AllowSet``'s "no empty or wildcard owner set";
* the grant id-sets (``granted_document_ids`` / ``granted_collection_ids``) are
  the **resolved** counterpart of the SQL grant ``EXISTS`` (spec 0004 §2.2): the
  retrieval chokepoint resolves the caller's grants from the ``grants`` table
  per request and passes the id-sets here, so a revoked grant (row deleted)
  vanishes from the set and the document is excluded again — deny-by-default.

:meth:`SearchAllowFilter.to_engine_filter` emits the filter as plain JSON-ready
dicts; :mod:`app.search.store` folds it into **both** legs of the hybrid query
(the BM25 ``bool.filter`` and the kNN ``filter``), so no ranked candidate is
ever produced outside the allow-set. Pure (no I/O), unit-testable with zero
mocks — the same reviewability argument as ``retrieval/permissions.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True, slots=True)
class SearchAllowFilter:
    """A principal's engine allow-filter: same tenant AND (owner OR granted).

    Built by the retrieval chokepoint (never by callers) from the resolved
    principal + their resolved grants. ``tenant_id`` and a non-empty
    ``owner_ids`` are required, so the deny-by-default shape is structural:
    omitting the tenant is a ``TypeError`` (missing argument), an empty owner
    set is a ``ValueError``, and the grant sets default to empty (grants only
    ever *widen*).
    """

    tenant_id: UUID
    owner_ids: frozenset[UUID]
    granted_document_ids: frozenset[UUID] = field(default_factory=frozenset)
    granted_collection_ids: frozenset[UUID] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.owner_ids:
            raise ValueError(
                "SearchAllowFilter requires a non-empty owner set "
                "(the requester always allows their own resources)."
            )

    def to_engine_filter(self) -> list[dict[str, object]]:
        """The filter clauses every engine query carries (INV-1 AND INV-2).

        ``[term tenant_id]`` AND ``[owner OR granted-doc OR granted-collection]``
        — the exact engine mirror of ``retrieval/queries._document_permitted``.
        Grant clauses are emitted only when their id-set is non-empty, but the
        tenant term and the owner clause are unconditional; ids are serialized
        as strings (the index maps them as ``keyword``). Sorted for a stable,
        assertable shape.
        """
        should: list[dict[str, object]] = [
            {"terms": {"owner_id": sorted(str(o) for o in self.owner_ids)}},
        ]
        if self.granted_document_ids:
            should.append(
                {"terms": {"document_id": sorted(str(d) for d in self.granted_document_ids)}}
            )
        if self.granted_collection_ids:
            should.append(
                {"terms": {"collection_id": sorted(str(c) for c in self.granted_collection_ids)}}
            )
        return [
            {"term": {"tenant_id": str(self.tenant_id)}},
            {"bool": {"should": should, "minimum_should_match": 1}},
        ]


__all__ = ["SearchAllowFilter"]
