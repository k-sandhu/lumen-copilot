"""The retrieval permission predicate — the INV-2 chokepoint (CC-1, spec 0004 §2.2).

This module computes, from the resolved :class:`~app.auth.principal.Principal`,
the **allow-set predicate** that every retrieval query in this package is keyed
off. It is pure (no I/O, no session) so the predicate itself is unit-testable
with zero mocks; the SQL builders in :mod:`app.retrieval.queries` translate it
into a ``WHERE`` clause and **every** search path applies it (there is no
unfiltered query — that is the whole point of the single chokepoint, ADR-0004).

**MVP allow-set (spec 0004 §2.2): deny by default.** "Permitted to see" =
``same tenant`` AND ``owner_id == principal.user_id``. The tenant scope is bound
separately (every query also filters ``tenant_id``, INV-1); this module owns the
**ownership** half. Explicit grants (the ``grants`` table), group/role
principals, and mirrored connector ACLs are decided in spec 0004 §2.2 but their
*build* is sequenced later (``revisit-at-implementation``); this predicate is the
seam they extend — a future grant-aware version widens the allowed-owner set
without any caller changing, because callers only ever see "the predicate".

The predicate is expressed as a small value object (:class:`AllowSet`) rather
than a raw column expression so it can be unit-tested in isolation (does owner X
satisfy it?) *and* compiled to SQL, and so a reviewer can see at a glance that
**ownership is required** — an empty or wildcard allow-set is impossible to
construct here.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.auth.principal import Principal


@dataclass(frozen=True, slots=True)
class AllowSet:
    """The set of resource owners a principal may retrieve from (deny-by-default).

    For the MVP this is exactly ``{principal.user_id}`` (a user sees only their
    own documents, spec 0004 §2.2). It carries the ``tenant_id`` too so the
    INV-1 tenant predicate and the INV-2 ownership predicate are derived from one
    object and can never drift apart. A future revision (explicit grants / group
    membership / mirrored ACL) returns an :class:`AllowSet` over a *wider* owner
    set; no caller changes because callers consume this object, not the rule.

    There is deliberately **no** constructor that yields an empty or wildcard
    owner set — the only way to build one is :meth:`for_principal`, which always
    includes the principal itself. Absence of a grant is denial.
    """

    tenant_id: UUID
    owner_ids: frozenset[UUID]

    @classmethod
    def for_principal(cls, principal: Principal) -> AllowSet:
        """Compute the MVP allow-set for ``principal`` (own documents only).

        Always includes the principal's own ``user_id`` and never the empty set,
        so a query keyed off this can never return another user's rows. The
        tenant comes from the token (``principal.tenant_id``), never request
        input (spec 0004 §2.3).
        """
        return cls(tenant_id=principal.tenant_id, owner_ids=frozenset({principal.user_id}))

    def permits_owner(self, owner_id: UUID) -> bool:
        """True iff a resource owned by ``owner_id`` is in the allow-set.

        The in-Python mirror of the SQL ``owner_id IN (:owner_ids)`` predicate —
        used by the tools to re-check a directly-fetched row and by the unit
        tests to assert the predicate fails closed for a foreign owner. Tenancy
        is checked separately (the repositories/queries bind ``tenant_id``); this
        is the ownership half only.
        """
        return owner_id in self.owner_ids
