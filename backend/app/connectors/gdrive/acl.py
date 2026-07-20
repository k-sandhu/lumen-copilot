"""The Drive effective-read ACL mapper — pure, fail-closed (ADR-0019 §2).

Implements the §2 effective-read algorithm **exactly**, one admit/deny rule per
clause, over the frozen :class:`~app.connectors.base.AclMappingContext` (the
framework's attested-identity snapshot). Pure: no I/O, no clock reads, no
module state — deterministic and unit-testable with zero mocks (one fixture
per case in the never-escalate suite).

An entry is admitted only when **all** of these hold:

* not ``deleted``;
* **no ``expirationTime`` at all** — v1 does not model time-boxed shares, so an
  entry carrying *any* expiration (future, past, or an explicit null) is denied
  and a mirror can never outlive a temporal grant;
* ``view`` **absent** (a ``view=metadata`` entry never grants content access);
* ``role`` ∈ the content-read set (owner/organizer/fileOrganizer/writer/
  commenter/reader);
* the ``type`` maps: ``user`` → ``user:<uuid>`` **iff** the email matches an
  **attested** tenant user (unattested/unknown ⇒ nothing); ``anyone`` → the
  tenant-wide principal (the subset proof holds even for guests);
  ``domain``/``group``/anything unknown ⇒ deny that entry.

**Inheritance reduction:** when the file sets
``inheritedPermissionsDisabled=true``, only entries whose ``permissionDetails``
mark them **direct** are considered — inherited entries still present in the
list are ignored; a missing/unreadable ``permissionDetails`` under that flag is
treated as *not provably direct* ⇒ ignored (fail closed). Otherwise the file's
effective list (direct + inherited) is consumed as-is; a parent's list is never
consulted directly.

**Any unknown type, role, or field state ⇒ deny that entry** — the mapped set
is provably ⊆ the source allow-list (never-escalate). The expiration/view rules
above test **key presence** rather than value truthiness precisely for that
reason: ``{"expirationTime": null}`` and ``{"view": ""}`` are states this
version does not model, and an unmodelled state denies.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.connectors.base import AclMappingContext

# The content-read role set (ADR-0019 §2). Anything else — including future
# roles Google may add — denies the entry (unknown ⇒ deny).
CONTENT_READ_ROLES = frozenset(
    {"owner", "organizer", "fileOrganizer", "writer", "commenter", "reader"}
)


def _is_direct(entry: Mapping[str, object]) -> bool:
    """True iff ``permissionDetails`` provably marks this entry direct.

    Used only under ``inheritedPermissionsDisabled=true``. Missing, malformed,
    or all-inherited details ⇒ NOT direct (fail closed — an entry that cannot
    prove directness is ignored under the reduction).
    """
    details = entry.get("permissionDetails")
    if not isinstance(details, list):
        return False
    for detail in details:
        if isinstance(detail, dict) and detail.get("inherited") is False:
            return True
    return False


def _map_entry(entry: Mapping[str, object], ctx: AclMappingContext) -> str | None:
    """Map ONE permission entry to a Lumen principal, or ``None`` (deny)."""
    if entry.get("deleted"):
        return None
    # ANY expiration present — future, past, or an explicit null/empty value —
    # denies (v1 has no acl_expires_at). **Key presence**, not truthiness: an
    # entry that carries the field at all is a time-boxed share whose exact
    # state we refuse to interpret, and an unknown state must fail closed.
    if "expirationTime" in entry:
        return None
    # A `view` entry (e.g. "metadata") never grants content access. Again by
    # key presence: `view: null` / `view: ""` are unknown states, not "unset".
    if "view" in entry:
        return None
    role = entry.get("role")
    if not isinstance(role, str) or role not in CONTENT_READ_ROLES:
        return None
    entry_type = entry.get("type")
    if entry_type == "user":
        email = entry.get("emailAddress")
        if not isinstance(email, str) or not email:
            return None
        # Attested emails only (the ctx snapshot holds nothing else).
        return ctx.principal_for_email(email)
    if entry_type == "anyone":
        # Public/link-public: every tenant member could read it at the source
        # regardless of email domain — the tenant principal is a strict subset.
        return ctx.tenant_principal
    # domain (no verified domain binding in v1), group (unexpanded), and any
    # unknown/future type ⇒ deny that entry.
    return None


def map_acl(raw: Mapping[str, object], ctx: AclMappingContext) -> frozenset[str]:
    """The Drive §2 effective-read mapping: raw permission list → principal set.

    ``raw`` carries the file's own effective permission list plus the
    file-level inheritance flag::

        {"permissions": [<entry>, ...], "inheritedPermissionsDisabled": bool}

    Anything unmappable grants nobody; the returned set may be empty (the
    document is then ingested but retrievable by no one — counted in
    ``unmapped_acl_count``, ADR-0019 §2 fail-closed rules).
    """
    permissions = raw.get("permissions")
    if not isinstance(permissions, list):
        return frozenset()
    reduce_to_direct = raw.get("inheritedPermissionsDisabled") is True
    principals: set[str] = set()
    for entry in permissions:
        if not isinstance(entry, Mapping):
            continue
        if reduce_to_direct and not _is_direct(entry):
            continue
        principal = _map_entry(entry, ctx)
        if principal is not None:
            principals.add(principal)
    return frozenset(principals)


__all__ = ["CONTENT_READ_ROLES", "map_acl"]
