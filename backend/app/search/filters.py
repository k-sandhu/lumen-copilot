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

**Mode-split (ADR-0019 §2, spec 0004 §2.2 exclusive enforcement).**
:meth:`SearchAllowFilter.to_engine_filter` mirrors the Postgres predicate
(``retrieval/queries._document_permitted``) exactly — the two chokepoints widen
in lockstep, nowhere else:

* ``acl_enforced=false`` (uploads, ``web``): today's owner/grant terms,
  unchanged;
* ``acl_enforced=true`` (managed-connector documents): access requires the
  mirrored ``acl_principals`` intersection **and** ``acl_synced_at`` within the
  freshness window — and nothing else. The owner and grant legs deliberately do
  NOT apply: an empty or stale mirror admits no one, including the owner.

The mandatory ``tenant_id`` term sits OUTSIDE the split, untouched (INV-1).
:mod:`app.search.store` folds the filter into **both** legs of the hybrid query
(the BM25 ``bool.filter`` and the kNN ``filter``), so no ranked candidate is
ever produced outside the allow-set. Pure (no I/O) apart from the cached
settings read for the freshness window — deterministic given a pinned ``now``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID


def acl_freshness_floor(now: datetime | None = None) -> datetime:
    """The oldest ``acl_synced_at`` still admissible (ADR-0019 §2).

    ``now − CONNECTOR_ACL_MAX_AGE_HOURS``: a mirrored ACL refreshed before this
    instant is stale ⇒ deny. The ONE definition both chokepoints key off —
    ``retrieval/queries._document_permitted`` (Postgres) and
    :meth:`SearchAllowFilter.to_engine_filter` (engine) import it, so the
    window can never drift between the two. ``now`` is injectable for tests.
    """
    from app.core.config import get_settings

    reference = now if now is not None else datetime.now(UTC)
    return reference - timedelta(hours=get_settings().connector_acl_max_age_hours)


@dataclass(frozen=True, slots=True)
class SearchAllowFilter:
    """A principal's engine allow-filter: same tenant AND the mode-split predicate.

    Built by the retrieval chokepoint (never by callers) from the resolved
    principal + their resolved grants. ``tenant_id`` and a non-empty
    ``owner_ids`` are required, so the deny-by-default shape is structural:
    omitting the tenant is a ``TypeError`` (missing argument), an empty owner
    set is a ``ValueError``, and the grant sets default to empty (grants only
    ever *widen* the non-enforced branch). ``acl_principals`` is the
    requester's mirrored-principal identity (``user:<uuid>`` + ``tenant``,
    ADR-0019 §2) — it gates ONLY the ``acl_enforced`` branch and defaults to
    empty (an empty set admits no enforced document — fail closed).
    """

    tenant_id: UUID
    owner_ids: frozenset[UUID]
    granted_document_ids: frozenset[UUID] = field(default_factory=frozenset)
    granted_collection_ids: frozenset[UUID] = field(default_factory=frozenset)
    acl_principals: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.owner_ids:
            raise ValueError(
                "SearchAllowFilter requires a non-empty owner set "
                "(the requester always allows their own resources)."
            )

    def to_engine_filter(self, *, now: datetime | None = None) -> list[dict[str, object]]:
        """The filter clauses every engine query carries (INV-1 AND INV-2).

        ``[term tenant_id]`` AND the **mode-split** bool — the exact engine
        mirror of ``retrieval/queries._document_permitted`` (ADR-0019 §2, the
        two chokepoints widen in lockstep):

        * non-enforced branch: ``acl_enforced=false`` AND (owner OR
          granted-doc OR granted-collection) — byte-identical to the pre-0019
          owner/grant terms;
        * enforced branch: ``acl_enforced=true`` AND the ``acl_principals``
          terms match AND ``acl_synced_at >= now − window`` (a missing/NULL
          ``acl_synced_at`` never satisfies the range — stale ⇒ deny).

        Grant clauses are emitted only when their id-set is non-empty, but the
        tenant term, the owner clause, and both mode branches are
        unconditional; ids are serialized as strings (the index maps them as
        ``keyword``). Sorted for a stable, assertable shape. ``now`` is
        injectable for tests; production uses the wall clock.
        """
        owner_or_grant: list[dict[str, object]] = [
            {"terms": {"owner_id": sorted(str(o) for o in self.owner_ids)}},
        ]
        if self.granted_document_ids:
            owner_or_grant.append(
                {"terms": {"document_id": sorted(str(d) for d in self.granted_document_ids)}}
            )
        if self.granted_collection_ids:
            owner_or_grant.append(
                {"terms": {"collection_id": sorted(str(c) for c in self.granted_collection_ids)}}
            )
        not_enforced_branch: dict[str, object] = {
            "bool": {
                "filter": [
                    {"term": {"acl_enforced": False}},
                    {"bool": {"should": owner_or_grant, "minimum_should_match": 1}},
                ]
            }
        }
        enforced_branch: dict[str, object] = {
            "bool": {
                "filter": [
                    {"term": {"acl_enforced": True}},
                    {"terms": {"acl_principals": sorted(self.acl_principals)}},
                    {"range": {"acl_synced_at": {"gte": acl_freshness_floor(now).isoformat()}}},
                ]
            }
        }
        return [
            {"term": {"tenant_id": str(self.tenant_id)}},
            {
                "bool": {
                    "should": [not_enforced_branch, enforced_branch],
                    "minimum_should_match": 1,
                }
            },
        ]


__all__ = ["SearchAllowFilter", "acl_freshness_floor"]
