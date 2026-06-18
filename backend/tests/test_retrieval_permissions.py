"""Unit tests for the retrieval permission predicate (issue #45, AC-2/AC-4, INV-2).

The permission predicate is the INV-2 chokepoint; it is pure, so it is tested
here with zero mocks. The headline negative: the allow-set built for user A never
permits user B's owner id — deny by default, and there is no way to construct an
empty/wildcard allow-set.
"""

from __future__ import annotations

import uuid

from app.auth.principal import Principal
from app.domain.entities import Role
from app.retrieval.permissions import AllowSet


def _principal(user_id: uuid.UUID, tenant_id: uuid.UUID) -> Principal:
    return Principal(user_id=user_id, tenant_id=tenant_id, roles=(Role.MEMBER,))


def test_allow_set_includes_own_owner_id() -> None:
    """A principal's allow-set permits exactly their own owner id (MVP)."""
    user = uuid.uuid4()
    tenant = uuid.uuid4()
    allow = AllowSet.for_principal(_principal(user, tenant))
    assert allow.tenant_id == tenant
    assert allow.owner_ids == frozenset({user})
    assert allow.permits_owner(user)


def test_allow_set_denies_another_user_same_tenant() -> None:
    """INV-2: a same-tenant other user's documents are NOT permitted (deny by default)."""
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    tenant = uuid.uuid4()
    allow = AllowSet.for_principal(_principal(user_a, tenant))
    assert not allow.permits_owner(user_b)


def test_allow_set_is_never_empty() -> None:
    """The only constructor always includes the principal — no empty allow-set."""
    user = uuid.uuid4()
    allow = AllowSet.for_principal(_principal(user, uuid.uuid4()))
    assert allow.owner_ids  # non-empty
    assert len(allow.owner_ids) == 1


def test_tenant_bound_from_principal_not_input() -> None:
    """The allow-set's tenant is the principal's tenant (token-bound, INV-1)."""
    tenant = uuid.uuid4()
    allow = AllowSet.for_principal(_principal(uuid.uuid4(), tenant))
    assert allow.tenant_id == tenant
