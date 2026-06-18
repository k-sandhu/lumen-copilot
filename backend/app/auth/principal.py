"""The resolved principal — "who is asking", and from which tenant.

A :class:`Principal` is the typed result of validating a bearer token in
``auth/`` (ADR-0004: this module is the *only* token validator). It is the seam
every downstream wave-2 endpoint consumes: routers receive it from the
``current_user`` dependency, and the tenant it carries — bound **at the token**,
never from request input (spec 0004 §2.3) — keys the ``db/`` repositories and the
``retrieval/`` permission filter.

Pure value object: no I/O, no framework imports (it lives under ``auth/`` rather
than ``domain/`` because it is auth's output type, but it follows the same
purity discipline).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.domain.entities import Role, User


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated identity resolved from an access token.

    Mirrors the JWT claims (spec 0004 §2.3): ``user_id`` ← ``sub``, plus
    ``tenant_id`` and ``roles``. Frozen so a handler cannot mutate the resolved
    identity mid-request.
    """

    user_id: UUID
    tenant_id: UUID
    roles: tuple[Role, ...]

    def has_role(self, role: Role) -> bool:
        """True if the principal holds ``role`` (RBAC check, spec 0004 §2.3)."""
        return role in self.roles

    @classmethod
    def from_user(cls, user: User) -> Principal:
        """Build a principal from a persisted user (the login/refresh path)."""
        return cls(user_id=user.id, tenant_id=user.tenant_id, roles=tuple(user.roles))
