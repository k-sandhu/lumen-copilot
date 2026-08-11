"""The ``gdrive`` subject — the kit's first (and today only) real member.

Binds the **real** :func:`app.connectors.gdrive.acl.map_acl` to the kit's
contract: one fixture per ADR-0019 §2 effective-read rule, plus the independent
source oracle and the fuzz generator the never-escalate property runs over.

Nothing here re-implements the mapper. :func:`_source_admits` is written from
**Drive's** semantics ("who could open this file at the source right now?"),
which is what makes ``mapped ⊆ source`` a real proof rather than a tautology.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from app.connectors.base import AclMappingContext
from app.connectors.gdrive.acl import CONTENT_READ_ROLES, map_acl

from .gdrive_probes import PROBES
from .subject import AclCase, AclSubject

# The kit's tenant directory. `alice` is attested (the connecting admin),
# `mallory` is a real tenant member who was never attested, `guest` is a
# same-tenant member whose email domain sits OUTSIDE the sharing domain — the
# ADR-0019 §2 domain-share proof depends on that user existing.
SHARING_DOMAIN = "acme.test"
ALICE = "alice@acme.test"
BOB = "bob@acme.test"
MALLORY = "mallory@acme.test"
GUEST = "guest@partner.test"

_IDS: dict[str, uuid.UUID] = {
    ALICE: uuid.UUID("11111111-1111-4111-8111-111111111111"),
    BOB: uuid.UUID("22222222-2222-4222-8222-222222222222"),
    MALLORY: uuid.UUID("33333333-3333-4333-8333-333333333333"),
    GUEST: uuid.UUID("44444444-4444-4444-8444-444444444444"),
}
# Only ALICE and BOB carry an identity attestation; MALLORY and GUEST do not.
_ATTESTED = {ALICE: _IDS[ALICE], BOB: _IDS[BOB]}

CONTEXT = AclMappingContext(
    email_to_user_id=dict(_ATTESTED),
    evaluated_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
)


def _raw(*entries: Mapping[str, object], direct_only: bool = False) -> dict[str, object]:
    """A Drive file's own effective permission list + the inheritance flag."""
    return {"permissions": list(entries), "inheritedPermissionsDisabled": direct_only}


def _user(email: str, **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {"type": "user", "role": "reader", "emailAddress": email}
    entry.update(overrides)
    return entry


def _direct(entry: dict[str, object]) -> dict[str, object]:
    return {**entry, "permissionDetails": [{"permissionType": "file", "inherited": False}]}


def _inherited(entry: dict[str, object]) -> dict[str, object]:
    return {**entry, "permissionDetails": [{"permissionType": "file", "inherited": True}]}


# --- the independent source oracle -------------------------------------------


def _entry_is_directly_readable_at_source(entry: Mapping[str, object]) -> bool:
    """Would Drive itself let this entry's principal open the content *now*?

    Deliberately **permissive** where the fixture cannot tell us (an unknown
    role might well be a reading role Google added later): the oracle must be a
    superset of the true source allow-list, or ``mapped ⊆ source`` would stop
    being a never-escalate proof and start being a coincidence.
    """
    if entry.get("deleted"):
        return False  # a revoked entry grants nothing at the source either
    expires = entry.get("expirationTime")
    if isinstance(expires, str) and expires:
        # A time-boxed share: the source allows it only until then. Past ⇒ gone.
        try:
            if datetime.fromisoformat(expires.replace("Z", "+00:00")) <= CONTEXT.evaluated_at:
                return False
        except ValueError:
            return False
    if entry.get("view") == "metadata":
        return False  # metadata-only: the source never serves content for it
    role = entry.get("role")
    if isinstance(role, str) and role in {"", "none"}:
        return False
    return True


def _source_admits(raw: Mapping[str, object]) -> frozenset[str]:
    """The Lumen principals Drive itself would let read this file.

    The never-escalate oracle. Note what it deliberately DOES admit:

    * an **unattested** tenant member matched by email — the human really can
      open the file at the source; Lumen under-shares because it cannot prove
      the binding, which is a *trust* rule, not a never-escalate one (the
      dedicated ``unattested_user`` fixture covers it instead);
    * an entry with an unknown role — unknowable from the fixture, so the
      superset direction is taken.

    And what it refuses: ``domain`` never yields the tenant-wide principal (a
    same-tenant guest outside the sharing domain would be source-denied), and
    ``group`` yields only members the fixture declares.
    """
    permissions = raw.get("permissions")
    if not isinstance(permissions, list):
        return frozenset()
    direct_only = raw.get("inheritedPermissionsDisabled") is True
    allowed: set[str] = set()
    for entry in permissions:
        if not isinstance(entry, Mapping):
            continue
        if direct_only and _provably_inherited(entry):
            continue  # the source ignores it too under the reduction
        if not _entry_is_directly_readable_at_source(entry):
            continue
        allowed |= _principals_the_source_would_admit(entry)
    return frozenset(allowed)


def _provably_inherited(entry: Mapping[str, object]) -> bool:
    """True only when every detail marks the entry inherited (else: unknowable)."""
    details = entry.get("permissionDetails")
    if not isinstance(details, list) or not details:
        return False
    return all(isinstance(d, Mapping) and d.get("inherited") is True for d in details)


def _principals_the_source_would_admit(entry: Mapping[str, object]) -> set[str]:
    """The Lumen principals ONE source-readable entry names."""
    kind = entry.get("type")
    if kind == "user":
        email = entry.get("emailAddress")
        user_id = _IDS.get(email.casefold()) if isinstance(email, str) else None
        return {f"user:{user_id}"} if user_id is not None else set()
    if kind == "anyone":
        return {"tenant"} | {f"user:{uid}" for uid in _IDS.values()}
    if kind == "domain":
        domain = entry.get("domain")
        if not isinstance(domain, str):
            return set()
        return {
            f"user:{uid}"
            for email, uid in _IDS.items()
            if email.rsplit("@", 1)[-1] == domain.casefold()
        }
    if kind == "group":
        members = entry.get("_members")
        if not isinstance(members, list):
            return set()
        found = (_IDS.get(str(m).casefold()) for m in members)
        return {f"user:{uid}" for uid in found if uid is not None}
    return set()


# --- the fuzz generator -------------------------------------------------------

_EMAILS = (ALICE, BOB, MALLORY, GUEST, "nobody@elsewhere.test")
_ROLES = (*sorted(CONTENT_READ_ROLES), "futureRole", "none", "")
_TYPES = ("user", "anyone", "domain", "group", "futureType")
_EXPIRIES = ("2099-01-01T00:00:00Z", "2001-01-01T00:00:00Z", "", None)
_VIEWS = ("metadata", "", None)


def _generate(rng: random.Random) -> dict[str, object]:
    """A random Drive permission list mixing every modelled and unmodelled state."""
    entries: list[dict[str, object]] = []
    for _ in range(rng.randint(0, 6)):
        kind = rng.choice(_TYPES)
        entry: dict[str, object] = {"type": kind, "role": rng.choice(_ROLES)}
        if kind == "user":
            entry["emailAddress"] = rng.choice(_EMAILS)
        elif kind == "domain":
            entry["domain"] = rng.choice((SHARING_DOMAIN, "partner.test", "elsewhere.test"))
        elif kind == "group":
            entry["emailAddress"] = "eng@acme.test"
            entry["_members"] = list(rng.sample(_EMAILS, rng.randint(0, 2)))
        if rng.random() < 0.25:
            entry["deleted"] = rng.choice((True, False))
        if rng.random() < 0.25:
            entry["expirationTime"] = rng.choice(_EXPIRIES)
        if rng.random() < 0.25:
            entry["view"] = rng.choice(_VIEWS)
        if rng.random() < 0.4:
            entry["permissionDetails"] = [
                {"permissionType": "file", "inherited": rng.choice((True, False))}
            ]
        entries.append(entry)
    return _raw(*entries, direct_only=rng.random() < 0.4)


# --- the subject --------------------------------------------------------------

_CASES: tuple[AclCase, ...] = (
    AclCase(
        id="direct_user",
        raw=_raw(_user(ALICE)),
        admits=frozenset({f"user:{_IDS[ALICE]}"}),
        why="an attested tenant user matched by case-folded email",
    ),
    AclCase(
        id="public",
        raw=_raw({"type": "anyone", "role": "reader"}),
        admits=frozenset({"tenant"}),
        why="link-public: every tenant member is a strict subset of the source audience",
    ),
    AclCase(
        id="empty_acl",
        raw=_raw(),
        admits=frozenset(),
        why="an empty mirror admits no one, including the owner",
    ),
    AclCase(
        id="group_only",
        raw=_raw({"type": "group", "role": "reader", "emailAddress": "eng@acme.test"}),
        admits=frozenset(),
        why="groups are unexpanded in v1 — under-sharing is the failure direction",
    ),
    AclCase(
        id="domain_only",
        raw=_raw({"type": "domain", "role": "reader", "domain": SHARING_DOMAIN}),
        admits=frozenset(),
        why="no verified workspace<->tenant domain binding; the tenant holds outside guests",
    ),
    AclCase(
        id="unattested_user",
        raw=_raw(_user(MALLORY)),
        admits=frozenset(),
        why="an email with no identity attestation maps to nothing",
    ),
    AclCase(
        id="revoked_entry",
        raw=_raw(_user(ALICE, deleted=True)),
        admits=frozenset(),
        why="a deleted permission entry grants nobody",
    ),
    AclCase(
        id="expiring_entry",
        raw=_raw(_user(ALICE, expirationTime="2099-01-01T00:00:00Z")),
        admits=frozenset(),
        why="ANY expirationTime denies — a mirror can never outlive a temporal grant",
    ),
    AclCase(
        id="metadata_only_entry",
        raw=_raw(_user(ALICE, view="metadata")),
        admits=frozenset(),
        why="a view=metadata entry never grants content access",
    ),
    AclCase(
        id="unknown_role",
        raw=_raw(_user(ALICE, role="futureRole")),
        admits=frozenset(),
        why="an unmodelled role denies that entry",
    ),
    AclCase(
        id="unknown_type",
        raw=_raw({"type": "futureType", "role": "reader"}),
        admits=frozenset(),
        why="an unmodelled principal type denies that entry",
    ),
    AclCase(
        id="inherited_under_reduction",
        raw=_raw(_inherited(_user(ALICE)), direct_only=True),
        admits=frozenset(),
        why="inheritedPermissionsDisabled ignores inherited entries still in the list",
    ),
    AclCase(
        id="limited_access_direct_only",
        raw=_raw(_direct(_user(ALICE)), _inherited(_user(BOB)), direct_only=True),
        admits=frozenset({f"user:{_IDS[ALICE]}"}),
        why="a limited-access file inside a shared folder admits only its direct principals",
    ),
    # --- extra fixtures beyond the required vocabulary ------------------------
    AclCase(
        id="case_folded_email",
        raw=_raw(_user("ALICE@Acme.TEST")),
        admits=frozenset({f"user:{_IDS[ALICE]}"}),
        why="email matching is case-folded",
    ),
    AclCase(
        id="past_expiry",
        raw=_raw(_user(ALICE, expirationTime="2001-01-01T00:00:00Z")),
        admits=frozenset(),
        why="a past expiry denies too (key presence, not truthiness)",
    ),
    AclCase(
        id="null_expiry",
        raw=_raw(_user(ALICE, expirationTime=None)),
        admits=frozenset(),
        why="an explicit null expiry is an unmodelled state ⇒ deny",
    ),
    AclCase(
        id="empty_expiry",
        raw=_raw(_user(ALICE, expirationTime="")),
        admits=frozenset(),
        why="an empty expiry string is an unmodelled state ⇒ deny",
    ),
    AclCase(
        id="null_view",
        raw=_raw(_user(ALICE, view=None)),
        admits=frozenset(),
        why="an explicit null view is present-but-unknown ⇒ deny",
    ),
    AclCase(
        id="empty_view",
        raw=_raw(_user(ALICE, view="")),
        admits=frozenset(),
        why="an empty view string is present-but-unknown ⇒ deny",
    ),
    AclCase(
        id="missing_role",
        raw=_raw({"type": "user", "emailAddress": ALICE}),
        admits=frozenset(),
        why="an entry with no role at all denies",
    ),
    AclCase(
        id="user_without_email",
        raw=_raw({"type": "user", "role": "reader"}),
        admits=frozenset(),
        why="a user entry carrying no email denies",
    ),
    AclCase(
        id="malformed_list",
        raw={"permissions": "garbage"},
        admits=frozenset(),
        why="a malformed permission list denies everything",
    ),
    AclCase(
        id="missing_acl_payload",
        raw={},
        admits=frozenset(),
        why=(
            "the ACL field is ABSENT, not empty: an unknown field state grants "
            "nobody (a mapper defaulting a missing list to the tenant principal "
            "would pass every other fixture)"
        ),
    ),
    AclCase(
        id="missing_inheritance_flag",
        raw={"permissions": [_user(ALICE)]},
        admits=frozenset({f"user:{_IDS[ALICE]}"}),
        why="an absent inheritance flag means no reduction — the list counts as-is",
    ),
    AclCase(
        id="no_details_under_reduction",
        raw=_raw(_user(ALICE), direct_only=True),
        admits=frozenset(),
        why="an entry that cannot PROVE directness is ignored under the reduction",
    ),
    AclCase(
        id="inheritance_enabled_consumes_list",
        raw=_raw(_inherited(_user(BOB))),
        admits=frozenset({f"user:{_IDS[BOB]}"}),
        why="without the flag the effective list (direct + inherited) is consumed as-is",
    ),
    # One case PER content-read role. A single payload carrying all of them
    # would still map to {alice} if only ONE role admitted — the union hides the
    # others — so each role is its own equality assertion.
    *[
        AclCase(
            id=f"content_read_role:{role}",
            raw=_raw(_user(ALICE, role=role)),
            admits=frozenset({f"user:{_IDS[ALICE]}"}),
            why=f"{role} is in the content-read set and admits on its own",
        )
        for role in sorted(CONTENT_READ_ROLES)
    ],
)


SUBJECT = AclSubject(
    name="gdrive",
    declared_map_acl=map_acl,
    context=CONTEXT,
    tenant_users=dict(_IDS),
    attested_email=ALICE,
    attested_user_id=_IDS[ALICE],
    unattested_email=MALLORY,
    guest_email=GUEST,
    sharing_domain=SHARING_DOMAIN,
    cases=_CASES,
    source_admits=_source_admits,
    generate=_generate,
    single_user_acl=lambda email: _raw(_user(email)),
    cascade_probes=PROBES,
)

__all__ = ["ALICE", "BOB", "CONTEXT", "GUEST", "MALLORY", "SHARING_DOMAIN", "SUBJECT"]
