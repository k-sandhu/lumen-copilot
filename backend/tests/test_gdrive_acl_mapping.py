"""Drive effective-read ACL mapping fixtures — one per admit/deny case (#453).

The ADR-0019 §2 algorithm, pinned exactly: ``map_acl`` is pure over the frozen
:class:`AclMappingContext` (zero mocks), and every deny rule has its own
fixture — the never-escalate suite the issue's acceptance criteria demand.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.connectors.base import AclMappingContext
from app.connectors.gdrive.acl import CONTENT_READ_ROLES, map_acl

_ALICE_ID = uuid.uuid4()
_BOB_ID = uuid.uuid4()

_CTX = AclMappingContext(
    email_to_user_id={"alice@acme.test": _ALICE_ID, "bob@acme.test": _BOB_ID},
    evaluated_at=datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
)


def _raw(*entries: dict[str, object], inherited_disabled: bool = False) -> dict[str, object]:
    return {
        "permissions": list(entries),
        "inheritedPermissionsDisabled": inherited_disabled,
    }


def _user(email: str, **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {"type": "user", "role": "reader", "emailAddress": email}
    entry.update(overrides)
    return entry


# --- admits -------------------------------------------------------------------


def test_attested_user_email_maps_to_user_principal() -> None:
    assert map_acl(_raw(_user("alice@acme.test")), _CTX) == frozenset({f"user:{_ALICE_ID}"})


def test_email_match_is_case_folded() -> None:
    assert map_acl(_raw(_user("ALICE@Acme.TEST")), _CTX) == frozenset({f"user:{_ALICE_ID}"})


def test_anyone_maps_to_tenant_principal() -> None:
    assert map_acl(_raw({"type": "anyone", "role": "reader"}), _CTX) == frozenset({"tenant"})


def test_every_content_read_role_admits() -> None:
    for role in sorted(CONTENT_READ_ROLES):
        got = map_acl(_raw(_user("alice@acme.test", role=role)), _CTX)
        assert got == frozenset({f"user:{_ALICE_ID}"}), role


# --- denies (each rule its own fixture) --------------------------------------


def test_unattested_email_maps_to_nothing() -> None:
    """An email NOT in the attested snapshot grants nobody (ADR-0019 §2)."""
    assert map_acl(_raw(_user("mallory@acme.test")), _CTX) == frozenset()


def test_deleted_entry_denies() -> None:
    assert map_acl(_raw(_user("alice@acme.test", deleted=True)), _CTX) == frozenset()


def test_future_expiration_denies() -> None:
    """ANY expirationTime — even future — denies (no acl_expires_at in v1)."""
    entry = _user("alice@acme.test", expirationTime="2099-01-01T00:00:00Z")
    assert map_acl(_raw(entry), _CTX) == frozenset()


def test_past_expiration_denies() -> None:
    entry = _user("alice@acme.test", expirationTime="2001-01-01T00:00:00Z")
    assert map_acl(_raw(entry), _CTX) == frozenset()


def test_null_expiration_denies() -> None:
    """Regression: an explicit ``expirationTime: null`` is an unknown state.

    The rule is **key presence**, not truthiness — a value-based check
    (``.get(...) is not None``) admitted an entry whose expiration field is
    present but null/empty, which is exactly the "unknown field state" the
    never-escalate rule denies (ADR-0019 §2).
    """
    assert map_acl(_raw(_user("alice@acme.test", expirationTime=None)), _CTX) == frozenset()


def test_empty_expiration_denies() -> None:
    assert map_acl(_raw(_user("alice@acme.test", expirationTime="")), _CTX) == frozenset()


def test_metadata_view_denies() -> None:
    """A ``view`` entry (e.g. metadata) never grants content access."""
    assert map_acl(_raw(_user("alice@acme.test", view="metadata")), _CTX) == frozenset()


def test_null_view_denies() -> None:
    """Regression: ``view: null`` is present-but-unknown ⇒ deny (not "unset")."""
    assert map_acl(_raw(_user("alice@acme.test", view=None)), _CTX) == frozenset()


def test_empty_view_denies() -> None:
    assert map_acl(_raw(_user("alice@acme.test", view="")), _CTX) == frozenset()


def test_unknown_role_denies() -> None:
    assert map_acl(_raw(_user("alice@acme.test", role="futureRole")), _CTX) == frozenset()


def test_missing_role_denies() -> None:
    entry: dict[str, object] = {"type": "user", "emailAddress": "alice@acme.test"}
    assert map_acl(_raw(entry), _CTX) == frozenset()


def test_domain_share_denies() -> None:
    """No verified domain binding in v1 — the tenant may hold outside guests."""
    entry: dict[str, object] = {"type": "domain", "role": "reader", "domain": "acme.test"}
    assert map_acl(_raw(entry), _CTX) == frozenset()


def test_group_share_denies() -> None:
    """Groups are unexpanded in v1 — under-sharing is the failure direction."""
    entry: dict[str, object] = {
        "type": "group",
        "role": "reader",
        "emailAddress": "eng@acme.test",
    }
    assert map_acl(_raw(entry), _CTX) == frozenset()


def test_unknown_type_denies() -> None:
    entry: dict[str, object] = {"type": "futureType", "role": "reader"}
    assert map_acl(_raw(entry), _CTX) == frozenset()


def test_user_entry_without_email_denies() -> None:
    entry: dict[str, object] = {"type": "user", "role": "reader"}
    assert map_acl(_raw(entry), _CTX) == frozenset()


def test_malformed_permission_list_denies_everything() -> None:
    assert map_acl({"permissions": "garbage"}, _CTX) == frozenset()
    assert map_acl({}, _CTX) == frozenset()


# --- inheritance reduction ----------------------------------------------------


def test_inherited_disabled_keeps_only_direct_entries() -> None:
    """A limited-access file inside a shared folder admits only its direct
    principals — inherited entries still present in the list are ignored."""
    direct = _user(
        "alice@acme.test", permissionDetails=[{"permissionType": "file", "inherited": False}]
    )
    inherited = _user(
        "bob@acme.test", permissionDetails=[{"permissionType": "file", "inherited": True}]
    )
    got = map_acl(_raw(direct, inherited, inherited_disabled=True), _CTX)
    assert got == frozenset({f"user:{_ALICE_ID}"})


def test_inherited_disabled_without_details_denies_entry() -> None:
    """An entry that cannot PROVE directness under the reduction is ignored."""
    no_details = _user("alice@acme.test")
    assert map_acl(_raw(no_details, inherited_disabled=True), _CTX) == frozenset()


def test_inheritance_enabled_consumes_effective_list_as_is() -> None:
    """Without the flag the file's effective list (direct + inherited) counts."""
    inherited = _user(
        "bob@acme.test", permissionDetails=[{"permissionType": "file", "inherited": True}]
    )
    assert map_acl(_raw(inherited), _CTX) == frozenset({f"user:{_BOB_ID}"})


# --- never-escalate property --------------------------------------------------


def test_mapped_set_is_subset_of_source_allow_list() -> None:
    """Every emitted principal traces to an admitted source entry — the mapper
    can only ever narrow, never widen (spec 0004 §2.2 never-escalates)."""
    entries: list[dict[str, object]] = [
        _user("alice@acme.test"),  # admit → user:alice
        _user("mallory@evil.test"),  # unattested → nothing
        {"type": "anyone", "role": "reader"},  # admit → tenant
        {"type": "domain", "role": "reader", "domain": "acme.test"},  # deny
        {"type": "group", "role": "reader", "emailAddress": "eng@acme.test"},  # deny
        _user("bob@acme.test", deleted=True),  # deny
        _user("bob@acme.test", expirationTime="2099-01-01T00:00:00Z"),  # deny
        _user("bob@acme.test", view="metadata"),  # deny
    ]
    got = map_acl(_raw(*entries), _CTX)
    # The maximal admissible set from the source's own allow-list:
    admissible = frozenset({f"user:{_ALICE_ID}", "tenant"})
    assert got <= admissible
    assert got == admissible  # and nothing admissible was dropped here


def test_empty_context_still_maps_anyone_but_no_users() -> None:
    empty = AclMappingContext(email_to_user_id={}, evaluated_at=datetime(2026, 7, 18, tzinfo=UTC))
    got = map_acl(_raw(_user("alice@acme.test"), {"type": "anyone", "role": "reader"}), empty)
    assert got == frozenset({"tenant"})
