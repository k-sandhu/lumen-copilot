"""The retrieval permission predicate — the INV-2 chokepoint (CC-1, spec 0004 §2.2).

This module computes, from the resolved :class:`~app.auth.principal.Principal`,
the **allow-set predicate** that every retrieval query in this package is keyed
off. It is pure (no I/O, no session) so the predicate itself is unit-testable
with zero mocks; the SQL builders in :mod:`app.retrieval.queries` translate it
into a ``WHERE`` clause and **every** search path applies it (there is no
unfiltered query — that is the whole point of the single chokepoint, ADR-0004).

**MVP allow-set (spec 0004 §2.2): deny by default.** "Permitted to see" =
``same tenant`` AND (``owner_id == principal.user_id`` **OR** an explicit grant to
the requester covers the resource). The tenant scope is bound separately (every
query also filters ``tenant_id``, INV-1); this module owns the **ownership +
grant** half. The grant half (the ``grants`` table, spec 0004 §2.2) is now built
(issue #18): the allow-set carries the requester's **grant-identity** — their own
``user_id`` as a ``user`` principal — so the SQL filter can add an ``EXISTS`` over
``grants`` for that principal alongside the owner predicate. Group/role principals
and mirrored connector ACLs remain decided-but-deferred (``revisit-at-
implementation``); they widen the same seam (a wider principal set) without any
caller changing, because callers only ever see "the predicate".

The predicate is expressed as a small value object (:class:`AllowSet`) rather
than a raw column expression so it can be unit-tested in isolation (does owner X
satisfy it?) *and* compiled to SQL, and so a reviewer can see at a glance that
**ownership-or-grant is required** — an empty or wildcard allow-set is impossible
to construct here. The grant half is enforced in SQL (an ``EXISTS`` correlated to
the document/collection), never in Python, so a document grant and a cascading
collection grant are both one query.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.auth.principal import Principal


@dataclass(frozen=True, slots=True)
class AllowSet:
    """A principal's retrieval allow-set: owned resources **or** granted ones.

    Carries two halves of "permitted to see" (deny-by-default), both keyed off the
    same token-bound principal so they can never drift:

    * ``owner_ids`` — the owners whose resources are in-set. For the MVP this is
      exactly ``{principal.user_id}`` (a user sees their own documents).
    * ``grant_principal_id`` — the requester's **grant identity**, i.e. the
      ``user`` principal id the SQL filter checks the ``grants`` table against
      (spec 0004 §2.2, issue #18). An explicit grant of a document — or a
      collection (which cascades to its documents) — to this principal admits that
      resource into the allow-set even though the requester does not own it.

    It carries the ``tenant_id`` too so the INV-1 tenant predicate, the INV-2
    ownership predicate, and the grant ``EXISTS`` are all derived from one object.
    A future revision (group/role membership / mirrored ACL) widens the principal
    set; no caller changes because callers consume this object, not the rule.

    There is deliberately **no** constructor that yields an empty or wildcard
    owner set — the only way to build one is :meth:`for_principal`, which always
    includes the principal itself (as both owner and grant principal). Absence of
    ownership *and* a grant is denial.
    """

    tenant_id: UUID
    owner_ids: frozenset[UUID]
    grant_principal_id: UUID

    @classmethod
    def for_principal(cls, principal: Principal) -> AllowSet:
        """Compute the MVP allow-set for ``principal`` (own + explicitly granted).

        Always includes the principal's own ``user_id`` as both the in-set owner
        and the grant principal, and never the empty set, so a query keyed off
        this returns the requester's own rows plus any resource explicitly granted
        to them — never another user's un-granted rows. The tenant comes from the
        token (``principal.tenant_id``), never request input (spec 0004 §2.3).
        """
        return cls(
            tenant_id=principal.tenant_id,
            owner_ids=frozenset({principal.user_id}),
            grant_principal_id=principal.user_id,
        )

    def permits_owner(self, owner_id: UUID) -> bool:
        """True iff a resource owned by ``owner_id`` is in the allow-set by ownership.

        The in-Python mirror of the SQL ``owner_id IN (:owner_ids)`` predicate —
        used by the unit tests to assert the ownership half fails closed for a
        foreign owner. The **grant** half is enforced only in SQL (an ``EXISTS``
        over ``grants`` correlated to the document/collection), so this method
        deliberately answers the ownership half only; a non-owner with a grant is
        admitted by the query, not by this check. Tenancy is checked separately
        (the repositories/queries bind ``tenant_id``).
        """
        return owner_id in self.owner_ids
