"""Structural pins on the two predicates themselves — connector-independent.

The behavioural proofs live in :mod:`tests.acl_kit.test_stores`, which executes
both chokepoints over a seeded corpus. These are the complementary *shape* pins:
they assert the exclusive split is structural rather than merely currently
well-behaved — the enforced branch carries no owner/grant terms **at all**, and
an empty requester principal set produces a clause that can match nothing.

Restores the coverage of ``test_acl_mode_split.py``'s engine-filter and
allow-set assertions (#453), which the kit's behavioural consolidation would
otherwise have dropped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.auth.principal import Principal
from app.domain.entities import Role
from app.retrieval.permissions import AllowSet
from app.search.filters import SearchAllowFilter, acl_freshness_floor


def _branches(clauses: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The (non-enforced, enforced) halves of the mode-split bool."""
    split = clauses[1]["bool"]
    assert split["minimum_should_match"] == 1
    not_enforced, enforced = split["should"]
    return not_enforced, enforced


def test_engine_filter_mode_split_shape() -> None:
    """Tenant term OUTSIDE the split; enforced branch = principals + freshness.

    The ``len(filters) == 3`` assertion is the load-bearing one: it is what
    makes "the owner and grant legs do not apply to a connector document"
    structural rather than a property of the current term order.
    """
    tenant, owner = uuid.uuid4(), uuid.uuid4()
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    allow = SearchAllowFilter(
        tenant_id=tenant,
        owner_ids=frozenset({owner}),
        acl_principals=frozenset({f"user:{owner}", "tenant"}),
    )
    clauses = allow.to_engine_filter(now=now)
    assert clauses[0] == {"term": {"tenant_id": str(tenant)}}  # INV-1, untouched
    not_enforced, enforced = _branches(clauses)

    assert not_enforced["bool"]["filter"][0] == {"term": {"acl_enforced": False}}
    assert not_enforced["bool"]["filter"][1]["bool"]["should"] == [
        {"terms": {"owner_id": [str(owner)]}}
    ]

    filters = enforced["bool"]["filter"]
    assert filters[0] == {"term": {"acl_enforced": True}}
    assert filters[1] == {"terms": {"acl_principals": sorted({f"user:{owner}", "tenant"})}}
    assert filters[2] == {"range": {"acl_synced_at": {"gte": acl_freshness_floor(now).isoformat()}}}
    assert len(filters) == 3, "the enforced branch must carry NO owner/grant terms"


def test_engine_filter_grant_sets_only_widen_the_non_enforced_branch() -> None:
    """Resolved Lumen grants appear in one branch only (exclusive modes)."""
    tenant, owner = uuid.uuid4(), uuid.uuid4()
    document, collection = uuid.uuid4(), uuid.uuid4()
    allow = SearchAllowFilter(
        tenant_id=tenant,
        owner_ids=frozenset({owner}),
        granted_document_ids=frozenset({document}),
        granted_collection_ids=frozenset({collection}),
    )
    not_enforced, enforced = _branches(allow.to_engine_filter())
    should = not_enforced["bool"]["filter"][1]["bool"]["should"]
    assert {"terms": {"document_id": [str(document)]}} in should
    assert {"terms": {"collection_id": [str(collection)]}} in should
    rendered = str(enforced["bool"]["filter"])
    assert "document_id" not in rendered and "collection_id" not in rendered


def test_engine_filter_empty_principal_set_admits_no_enforced_document() -> None:
    """A defaulted (empty) principal set yields an un-matchable terms clause."""
    allow = SearchAllowFilter(tenant_id=uuid.uuid4(), owner_ids=frozenset({uuid.uuid4()}))
    _, enforced = _branches(allow.to_engine_filter())
    assert enforced["bool"]["filter"][1] == {"terms": {"acl_principals": []}}


def test_allow_set_derives_the_mirrored_principals_from_the_token() -> None:
    """``AllowSet.for_principal`` is where the requester's mirror identity comes
    from — the token-bound user plus the tenant-wide principal, and nothing a
    caller supplied."""
    principal = Principal(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), roles=(Role.MEMBER,))
    allow = AllowSet.for_principal(principal)
    assert allow.acl_principals == frozenset({f"user:{principal.user_id}", "tenant"})
    assert allow.tenant_id == principal.tenant_id
    assert allow.owner_ids == frozenset({principal.user_id})
