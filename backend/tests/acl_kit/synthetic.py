"""A synthetic ACL-declaring connector — the kit's connector-agnosticism proof.

Nothing about ``lumen-fake`` resembles Drive: its raw payload is ``{"grants":
[...], "direct_only": bool}``, its entries are ``kind``/``level``/``state``
rather than ``type``/``role``/``deleted``, its expiry field is ``until`` and its
metadata-only marker is ``scope: "preview"``. If any kit test only passes for
``gdrive``, this subject fails it — which is exactly the guarantee the issue
asks for: **the next managed connector adds a fixture, not a test file.**

The mapper below is a *test double of a connector*, not of the framework: it
implements the same ADR-0019 §2 deny-by-default rules in its own vocabulary, so
the kit's proofs are about the rules, never about Drive's field names.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime

from app.connectors.base import (
    AclMappingContext,
    ConnectorError,
    FetchedDoc,
    PageIntegrity,
    SourceAcl,
    SyncPage,
)

from .subject import AclCase, AclSubject, CascadeProbe

SHARING_DOMAIN = "widgets.test"
ADA = "ada@widgets.test"
LIN = "lin@widgets.test"
NOAH = "noah@widgets.test"  # tenant member, never attested
VISITOR = "visitor@outside.test"  # same-tenant guest, outside the sharing domain

_IDS: dict[str, uuid.UUID] = {
    ADA: uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001"),
    LIN: uuid.UUID("aaaaaaaa-0000-4000-8000-000000000002"),
    NOAH: uuid.UUID("aaaaaaaa-0000-4000-8000-000000000003"),
    VISITOR: uuid.UUID("aaaaaaaa-0000-4000-8000-000000000004"),
}
_ATTESTED = {ADA: _IDS[ADA], LIN: _IDS[LIN]}

CONTEXT = AclMappingContext(
    email_to_user_id=dict(_ATTESTED),
    evaluated_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
)

# The connector's own content-read vocabulary.
READ_LEVELS = frozenset({"own", "edit", "comment", "read"})


def _raw(*grants: Mapping[str, object], direct_only: bool = False) -> dict[str, object]:
    return {"grants": list(grants), "direct_only": direct_only}


def _person(mail: str, **overrides: object) -> dict[str, object]:
    grant: dict[str, object] = {"kind": "person", "level": "read", "mail": mail}
    grant.update(overrides)
    return grant


def _origin(grant: Mapping[str, object], origin: str) -> dict[str, object]:
    return {**grant, "origin": origin}


# --- the connector's fail-closed mapper --------------------------------------


def map_acl(raw: Mapping[str, object], ctx: AclMappingContext) -> frozenset[str]:
    """``lumen-fake``'s pure, fail-closed ACL mapper (ADR-0019 §2/§4)."""
    grants = raw.get("grants")
    if not isinstance(grants, list):
        return frozenset()
    direct_only = raw.get("direct_only") is True
    principals: set[str] = set()
    for grant in grants:
        if not isinstance(grant, Mapping):
            continue
        if direct_only and grant.get("origin") != "direct":
            continue  # cannot prove directness ⇒ ignored under the reduction
        principal = _map_grant(grant, ctx)
        if principal is not None:
            principals.add(principal)
    return frozenset(principals)


def _map_grant(grant: Mapping[str, object], ctx: AclMappingContext) -> str | None:
    if grant.get("state") == "revoked":
        return None
    if "until" in grant:  # ANY expiry present ⇒ deny (no acl_expires_at in v1)
        return None
    if "scope" in grant:  # e.g. a preview/metadata-only grant
        return None
    level = grant.get("level")
    if not isinstance(level, str) or level not in READ_LEVELS:
        return None
    kind = grant.get("kind")
    if kind == "person":
        mail = grant.get("mail")
        if not isinstance(mail, str) or not mail:
            return None
        return ctx.principal_for_email(mail)
    if kind == "everyone":
        return ctx.tenant_principal
    return None  # team (unexpanded), org (no verified binding), anything unknown


# --- the independent source oracle -------------------------------------------


def _readable_at_source(grant: Mapping[str, object]) -> bool:
    if grant.get("state") == "revoked":
        return False
    until = grant.get("until")
    if isinstance(until, str) and until:
        try:
            if datetime.fromisoformat(until.replace("Z", "+00:00")) <= CONTEXT.evaluated_at:
                return False
        except ValueError:
            return False
    if grant.get("scope") == "preview":
        return False
    level = grant.get("level")
    return not (isinstance(level, str) and level in {"", "none"})


def _grant_principals(grant: Mapping[str, object]) -> set[str]:
    kind = grant.get("kind")
    if kind == "person":
        mail = grant.get("mail")
        user_id = _IDS.get(mail.casefold()) if isinstance(mail, str) else None
        return {f"user:{user_id}"} if user_id is not None else set()
    if kind == "everyone":
        return {"tenant"} | {f"user:{uid}" for uid in _IDS.values()}
    if kind == "org":
        org = grant.get("org")
        if not isinstance(org, str):
            return set()
        return {
            f"user:{uid}" for mail, uid in _IDS.items() if mail.rsplit("@", 1)[-1] == org.casefold()
        }
    if kind == "team":
        members = grant.get("_members")
        if not isinstance(members, list):
            return set()
        found = (_IDS.get(str(m).casefold()) for m in members)
        return {f"user:{uid}" for uid in found if uid is not None}
    return set()


def _source_admits(raw: Mapping[str, object]) -> frozenset[str]:
    grants = raw.get("grants")
    if not isinstance(grants, list):
        return frozenset()
    direct_only = raw.get("direct_only") is True
    allowed: set[str] = set()
    for grant in grants:
        if not isinstance(grant, Mapping):
            continue
        if direct_only and grant.get("origin") == "inherited":
            continue
        if not _readable_at_source(grant):
            continue
        allowed |= _grant_principals(grant)
    return frozenset(allowed)


# --- the fuzz generator -------------------------------------------------------

_MAILS = (ADA, LIN, NOAH, VISITOR, "stranger@nowhere.test")
_LEVELS = (*sorted(READ_LEVELS), "admin", "none", "")
_KINDS = ("person", "everyone", "org", "team", "singularity")


def _generate(rng: random.Random) -> dict[str, object]:
    grants: list[dict[str, object]] = []
    for _ in range(rng.randint(0, 6)):
        kind = rng.choice(_KINDS)
        grant: dict[str, object] = {"kind": kind, "level": rng.choice(_LEVELS)}
        if kind == "person":
            grant["mail"] = rng.choice(_MAILS)
        elif kind == "org":
            grant["org"] = rng.choice((SHARING_DOMAIN, "outside.test", "nowhere.test"))
        elif kind == "team":
            grant["_members"] = list(rng.sample(_MAILS, rng.randint(0, 2)))
        if rng.random() < 0.25:
            grant["state"] = rng.choice(("active", "revoked"))
        if rng.random() < 0.25:
            grant["until"] = rng.choice(("2099-01-01T00:00:00Z", "2001-01-01T00:00:00Z", "", None))
        if rng.random() < 0.25:
            grant["scope"] = rng.choice(("preview", "", None))
        if rng.random() < 0.4:
            grant["origin"] = rng.choice(("direct", "inherited"))
        grants.append(grant)
    return _raw(*grants, direct_only=rng.random() < 0.4)


# --- the subject --------------------------------------------------------------

_CASES: tuple[AclCase, ...] = (
    AclCase(
        id="direct_user",
        raw=_raw(_person(ADA)),
        admits=frozenset({f"user:{_IDS[ADA]}"}),
        why="an attested tenant user matched by mail",
    ),
    AclCase(
        id="public",
        raw=_raw({"kind": "everyone", "level": "read"}),
        admits=frozenset({"tenant"}),
        why="a public grant maps to the tenant-wide principal",
    ),
    AclCase(id="empty_acl", raw=_raw(), admits=frozenset(), why="an empty mirror admits no one"),
    AclCase(
        id="group_only",
        raw=_raw({"kind": "team", "level": "read", "team_id": "t-1"}),
        admits=frozenset(),
        why="teams are unexpanded",
    ),
    AclCase(
        id="domain_only",
        raw=_raw({"kind": "org", "level": "read", "org": SHARING_DOMAIN}),
        admits=frozenset(),
        why="no verified org<->tenant binding; the tenant holds outside visitors",
    ),
    AclCase(
        id="unattested_user",
        raw=_raw(_person(NOAH)),
        admits=frozenset(),
        why="a mail with no identity attestation maps to nothing",
    ),
    AclCase(
        id="revoked_entry",
        raw=_raw(_person(ADA, state="revoked")),
        admits=frozenset(),
        why="a revoked grant grants nobody",
    ),
    AclCase(
        id="expiring_entry",
        raw=_raw(_person(ADA, until="2099-01-01T00:00:00Z")),
        admits=frozenset(),
        why="ANY expiry denies",
    ),
    AclCase(
        id="metadata_only_entry",
        raw=_raw(_person(ADA, scope="preview")),
        admits=frozenset(),
        why="a preview-scoped grant never grants content access",
    ),
    AclCase(
        id="unknown_role",
        raw=_raw(_person(ADA, level="admin")),
        admits=frozenset(),
        why="an unmodelled level denies",
    ),
    AclCase(
        id="unknown_type",
        raw=_raw({"kind": "singularity", "level": "read"}),
        admits=frozenset(),
        why="an unmodelled kind denies",
    ),
    AclCase(
        id="inherited_under_reduction",
        raw=_raw(_origin(_person(ADA), "inherited"), direct_only=True),
        admits=frozenset(),
        why="direct_only ignores inherited grants still present in the list",
    ),
    AclCase(
        id="limited_access_direct_only",
        raw=_raw(
            _origin(_person(ADA), "direct"), _origin(_person(LIN), "inherited"), direct_only=True
        ),
        admits=frozenset({f"user:{_IDS[ADA]}"}),
        why="a limited-access item admits only its direct principals",
    ),
    AclCase(
        id="malformed_list",
        raw={"grants": "garbage"},
        admits=frozenset(),
        why="a malformed grant list denies everything",
    ),
    AclCase(
        id="missing_acl_payload",
        raw={},
        admits=frozenset(),
        why="the grant field is ABSENT, not empty: an unknown field state grants nobody",
    ),
    # One case per read level — see the gdrive subject for why the union form
    # would be weaker.
    *[
        AclCase(
            id=f"content_read_role:{level}",
            raw=_raw(_person(ADA, level=level)),
            admits=frozenset({f"user:{_IDS[ADA]}"}),
            why=f"{level} is in the read set and admits on its own",
        )
        for level in sorted(READ_LEVELS)
    ],
)


# --- a genuine (miniature) change-replay capability ---------------------------
#
# The cascade probes must induce REAL failures, not script a pre-formed
# ``INCOMPLETE`` page — that would re-prove the framework's reaction and call it
# a producer test. So this connector carries an actual container tree, an actual
# per-run re-examination budget, and an enumeration step that can actually fail;
# its probes then break those, exactly as the gdrive probes break Drive's.

CASCADE_REFETCH_BUDGET = 8


class SourceTree:
    """An in-memory container tree the fake connector enumerates."""

    def __init__(self) -> None:
        self.children: dict[str, list[str]] = {}
        self.fail_enumeration_of: set[str] = set()

    def add(self, container: str, *items: str) -> None:
        self.children.setdefault(container, []).extend(items)

    def enumerate(self, container: str) -> list[str]:
        if container in self.fail_enumeration_of:
            raise ConnectorError("subtree listing failed", code="enumerate_failed")
        return list(self.children.get(container, ()))


async def fetch_changes(
    tree: SourceTree, changed_container: str, *, budget: int = CASCADE_REFETCH_BUDGET
) -> AsyncIterator[SyncPage]:
    """Replay one container permission change (the §3 cascade path).

    Emits the container in ``stale_scope_ids`` and re-examines its descendants
    within ``budget``. A failed enumeration or an over-budget subtree leaves the
    affected set unprovable ⇒ ``integrity=incomplete`` (fail closed).
    """
    complete = True
    upserts: list[FetchedDoc] = []
    try:
        descendants = tree.enumerate(changed_container)
    except ConnectorError:
        descendants, complete = [], False  # enumeration failure ⇒ unprovable
    for used, item in enumerate(descendants):
        if used >= budget:
            complete = False  # budget exhausted ⇒ unprovable
            break
        upserts.append(
            FetchedDoc(
                title=item,
                text=f"{item} body",
                url=f"https://fake.invalid/{item}",
                external_id=item,
                acl=SourceAcl(
                    principals=frozenset({"tenant"}), scope_ids=frozenset({changed_container})
                ),
            )
        )
    yield SyncPage(
        upserts=tuple(upserts),
        deleted_external_ids=frozenset(),
        next_cursor="cursor-2",
        stale_scope_ids=frozenset({changed_container}),
        integrity=PageIntegrity.COMPLETE if complete else PageIntegrity.INCOMPLETE,
    )


async def _drain(tree: SourceTree, *, budget: int = CASCADE_REFETCH_BUDGET) -> list[SyncPage]:
    return [page async for page in fetch_changes(tree, "folder-1", budget=budget)]


async def _induce_enumeration_failure() -> list[SyncPage]:
    tree = SourceTree()
    tree.add("folder-1", "a", "b")
    tree.fail_enumeration_of.add("folder-1")
    return await _drain(tree)


async def _induce_budget_exhaustion() -> list[SyncPage]:
    tree = SourceTree()
    tree.add("folder-1", *[f"item{i}" for i in range(CASCADE_REFETCH_BUDGET + 1)])
    return await _drain(tree)


async def _induce_healthy_cascade() -> list[SyncPage]:
    tree = SourceTree()
    tree.add("folder-1", "a", "b")
    return await _drain(tree)


PROBES: dict[str, CascadeProbe] = {
    "enumeration_failure": CascadeProbe(
        id="enumeration_failure",
        why="the subtree listing of the changed container raises",
        induce=_induce_enumeration_failure,
    ),
    "budget_exhausted": CascadeProbe(
        id="budget_exhausted",
        why="the changed container holds more descendants than one run may re-examine",
        induce=_induce_budget_exhaustion,
    ),
    "healthy_cascade": CascadeProbe(
        id="healthy_cascade",
        why="control: an in-budget, fully enumerable cascade stays complete",
        induce=_induce_healthy_cascade,
    ),
}


SUBJECT = AclSubject(
    name="lumen-fake",
    declared_map_acl=map_acl,
    context=CONTEXT,
    tenant_users=dict(_IDS),
    attested_email=ADA,
    attested_user_id=_IDS[ADA],
    unattested_email=NOAH,
    guest_email=VISITOR,
    sharing_domain=SHARING_DOMAIN,
    cases=_CASES,
    source_admits=_source_admits,
    generate=_generate,
    single_user_acl=lambda mail: _raw(_person(mail)),
    cascade_probes=PROBES,
)

__all__ = ["ADA", "CONTEXT", "LIN", "NOAH", "SHARING_DOMAIN", "SUBJECT", "VISITOR", "map_acl"]
