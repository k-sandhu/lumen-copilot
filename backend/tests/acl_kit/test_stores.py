"""INV-1/INV-2 in **both** chokepoints, in parity — for every ACL connector.

ADR-0019 §2's exclusive enforcement modes, proven where they are enforced:
``retrieval/queries._document_permitted`` (Postgres) and
``search/filters.SearchAllowFilter`` (the engine), driven over one corpus seeded
from each connector's **own** ``map_acl`` output.

The headline is :func:`test_postgres_and_engine_agree_for_every_viewer`: the two
stores must return the *same* documents as each other **and** as an independent
restatement of the rule. A drift in either predicate breaks it; a drift in both
still breaks it against the oracle.

Subsumes the document-visibility matrix of ``tests/test_acl_mode_split.py``
(#453), which this file replaces.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import app.db.session as db_session
from app.retrieval import queries
from app.retrieval.permissions import AllowSet
from app.search.filters import SearchAllowFilter, acl_freshness_floor

from .subject import AclSubject
from .subjects import SUBJECT_IDS, SUBJECTS
from .world import (
    GRANTED_ENFORCED,
    REVOKED_AT_SOURCE,
    STALE_BEYOND_WINDOW,
    STALE_STAMPED,
    UPLOAD,
    World,
    raw_document,
)

pytestmark = pytest.mark.parametrize("subject", SUBJECTS, ids=SUBJECT_IDS)


async def _pg_visible(
    world: World, user_id: object, *, tenant_id: object | None = None
) -> set[str]:
    """Document keys the REAL Postgres predicate surfaces for this user."""
    allow = world.allow_set(user_id, tenant_id=tenant_id)  # type: ignore[arg-type]
    async with db_session.session_scope() as session:
        rows = await queries.list_documents(session, allow_set=allow, k=200)
    return {row.document_name.removesuffix(".txt") for row in rows}


# --- parity: the whole point --------------------------------------------------


async def test_postgres_and_engine_agree_for_every_viewer(
    world: World, subject: AclSubject
) -> None:
    """The two chokepoints are lockstepped, and both match the ADR's rule.

    Runs every seeded viewer through both stores. Three-way agreement means a
    silent widening of either predicate — or of both together — fails here.
    """
    for email, user_id in world.users.items():
        expected = world.expected_visible(user_id)
        assert await _pg_visible(world, user_id) == expected, f"postgres drift for {email}"
        assert await world.engine_visible(user_id) == expected, f"engine drift for {email}"


async def test_owner_is_denied_on_empty_and_stale_mirrors(
    world: World, subject: AclSubject
) -> None:
    """INV-2: ownership NEVER widens an ``acl_enforced`` document.

    The connecting admin owns every connector row, so this is the escalation the
    exclusive split exists to prevent — proven in Postgres AND the engine.
    """
    owner = world.owner_id
    for store_name, visible in (
        ("postgres", await _pg_visible(world, owner)),
        ("engine", await world.engine_visible(owner)),
    ):
        assert "case:empty_acl" not in visible, store_name
        assert REVOKED_AT_SOURCE not in visible, store_name
        assert STALE_BEYOND_WINDOW not in visible, store_name
        assert STALE_STAMPED not in visible, store_name
        # ...while the owner's own non-connector upload is untouched.
        assert UPLOAD in visible, store_name


async def test_explicit_lumen_grantee_is_denied_on_an_enforced_document(
    world: World, subject: AclSubject
) -> None:
    """INV-2: a Lumen grant can never admit a user the source denies.

    The same grantee holds a grant on the enforced (empty-mirror) document and
    on a plain upload: the upload is admitted, the connector document is not.
    """
    grantee = world.user_id(subject.guest_email)
    for store_name, visible in (
        ("postgres", await _pg_visible(world, grantee)),
        ("engine", await world.engine_visible(grantee)),
    ):
        assert GRANTED_ENFORCED not in visible, f"{store_name}: grant widened an enforced doc"
        assert UPLOAD in visible, f"{store_name}: the grant leg must still work where it applies"


async def test_group_only_and_domain_only_shares_reach_nobody(
    world: World, subject: AclSubject
) -> None:
    """Unexpanded groups and domain-restricted shares admit no one, in both stores."""
    for key in ("case:group_only", "case:domain_only"):
        for email, user_id in world.users.items():
            assert key not in await _pg_visible(world, user_id), f"{key} visible to {email} (pg)"
            assert key not in await world.engine_visible(user_id), f"{key} visible to {email}"


async def test_domain_share_is_invisible_to_a_same_tenant_guest(
    world: World, subject: AclSubject
) -> None:
    """The ADR's guest proof: a tenant member outside the sharing domain.

    A ``domain`` share mapped to the tenant-wide principal would expose the file
    to exactly this user, whom the source denies.
    """
    guest = world.user_id(subject.guest_email)
    assert subject.guest_email.rsplit("@", 1)[-1] != subject.sharing_domain
    assert "case:domain_only" not in await _pg_visible(world, guest)
    assert "case:domain_only" not in await world.engine_visible(guest)


async def test_unattested_email_match_reaches_nobody(world: World, subject: AclSubject) -> None:
    """An unattested identity is a Lumen user, and still sees nothing."""
    unattested = world.user_id(subject.unattested_email)
    assert "case:unattested_user" not in await _pg_visible(world, unattested)
    assert "case:unattested_user" not in await world.engine_visible(unattested)
    # ...and nobody else picked it up either.
    for user_id in world.users.values():
        assert "case:unattested_user" not in await _pg_visible(world, user_id)


async def test_freshness_window_denies_stale_mirrors(world: World, subject: AclSubject) -> None:
    """``acl_synced_at`` beyond ``CONNECTOR_ACL_MAX_AGE_HOURS`` ⇒ deny.

    Both a mirror that merely aged out and one actively stale-stamped (NULL by
    the §3 cascade) are denied — the stamped one immediately, not at expiry.
    """
    listed = world.user_id(subject.attested_email)
    assert f"user:{listed}" in world.docs[STALE_BEYOND_WINDOW].principals
    for store_name, visible in (
        ("postgres", await _pg_visible(world, listed)),
        ("engine", await world.engine_visible(listed)),
    ):
        assert STALE_BEYOND_WINDOW not in visible, store_name
        assert STALE_STAMPED not in visible, store_name


async def test_public_share_admits_every_tenant_member_including_guests(
    world: World, subject: AclSubject
) -> None:
    """The one provably-safe wildcard: ``anyone`` ⇒ the tenant principal."""
    for user_id in world.users.values():
        assert "case:public" in await _pg_visible(world, user_id)
        assert "case:public" in await world.engine_visible(user_id)


async def test_the_mapped_principal_is_the_only_one_admitted(
    world: World, subject: AclSubject
) -> None:
    """The attested user in the mirror sees it; nobody else in the tenant does."""
    listed = world.user_id(subject.attested_email)
    assert "case:direct_user" in await _pg_visible(world, listed)
    assert "case:direct_user" in await world.engine_visible(listed)
    for email, user_id in world.users.items():
        if user_id == listed:
            continue
        assert "case:direct_user" not in await _pg_visible(world, user_id), email
        assert "case:direct_user" not in await world.engine_visible(user_id), email


# --- INV-1: tenancy sits outside the mode split -------------------------------


async def test_foreign_tenant_sees_nothing_even_with_matching_principals(
    world: World, subject: AclSubject
) -> None:
    """INV-1: the tenant term is outside the split and cannot be widened.

    The foreign requester is handed the *listed* tenant's principal strings — a
    mirror intersection would match — and still gets nothing, in both stores.
    """
    listed = world.user_id(subject.attested_email)
    foreign_allow = AllowSet(
        tenant_id=world.foreign_tenant_id,
        owner_ids=frozenset({world.foreign_user_id}),
        grant_principal_id=world.foreign_user_id,
        acl_principals=frozenset({f"user:{listed}", "tenant"}),
    )
    async with db_session.session_scope() as session:
        rows = await queries.list_documents(session, allow_set=foreign_allow, k=200)
    assert rows == []

    foreign_filter = SearchAllowFilter(
        tenant_id=world.foreign_tenant_id,
        owner_ids=frozenset({world.foreign_user_id}),
        acl_principals=frozenset({f"user:{listed}", "tenant"}),
    )
    store = world.store()
    try:
        hits = await store.hybrid_search(
            query_text="anything", embedding=[0.1] * 8, allow=foreign_filter, k=100
        )
    finally:
        await store.aclose()
    assert hits == []


async def test_engine_filter_always_carries_the_tenant_term(
    world: World, subject: AclSubject
) -> None:
    """Every engine query this kit issued began with the mandatory tenant term."""
    await world.engine_visible(world.owner_id)
    assert world.engine.searches, "no engine query was recorded"
    for clauses in world.engine.searches:
        assert clauses[0] == {"term": {"tenant_id": str(world.tenant_id)}}


# --- the indexed shape --------------------------------------------------------


async def test_enforced_documents_index_their_mirror_explicitly(
    world: World, subject: AclSubject
) -> None:
    """``acl_enforced`` is an explicit boolean on every indexed chunk.

    An empty keyword array indexes like a missing field, so the mode could not
    otherwise discriminate "not a connector document" from "connector document
    nobody may see" (ADR-0019 §2).
    """
    empty_mirror = raw_document(world, REVOKED_AT_SOURCE)
    assert empty_mirror["acl_enforced"] is True
    assert empty_mirror["acl_principals"] == []
    assert empty_mirror["acl_synced_at"] is not None

    stamped = raw_document(world, STALE_STAMPED)
    assert stamped["acl_enforced"] is True
    assert stamped["acl_synced_at"] is None  # stale-stamped ⇒ the range denies it

    upload = raw_document(world, UPLOAD)
    assert upload["acl_enforced"] is False
    assert upload["acl_principals"] == []


async def test_freshness_floor_is_shared_by_both_chokepoints(
    world: World, subject: AclSubject
) -> None:
    """One definition of the window — the engine range uses the same floor.

    Both sides call ``acl_freshness_floor()``, which is relative to *now*, so
    the assertion is a tolerance rather than equality: what matters is that the
    engine's floor is the shared helper's value and not some second definition
    (a hard-coded window here would be off by hours, not by milliseconds).
    """
    await world.engine_visible(world.owner_id)
    enforced_branch = world.engine.searches[-1][1]["bool"]["should"][1]
    floor = enforced_branch["bool"]["filter"][2]["range"]["acl_synced_at"]["gte"]
    drift = abs(datetime.fromisoformat(floor) - acl_freshness_floor())
    assert drift < timedelta(minutes=1), f"engine floor {floor} is not the shared window"
