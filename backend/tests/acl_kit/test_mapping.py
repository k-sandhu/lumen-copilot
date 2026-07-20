"""ADR-0019 §2 effective-read mapping — one fixture per rule, every connector.

Parametrized over :data:`tests.acl_kit.subjects.SUBJECTS`, so each proof runs
against the real ``gdrive`` mapper **and** the synthetic connector. Three layers:

1. the **required-case gate** — a connector may not join the kit without a
   fixture for every deny rule in the ADR's table;
2. the **fixtures themselves** — each case is an equality assertion, so an extra
   principal fails exactly as loudly as a missing one;
3. the **never-escalate property** — over generated permission payloads, the
   mapped set is provably ⊆ what the source itself would allow.

Subsumes the per-connector fixture file ``tests/test_gdrive_acl_mapping.py``
(#453), whose cases now live in :mod:`tests.acl_kit.gdrive` as subject data.
"""

from __future__ import annotations

import random
from collections.abc import Mapping

import pytest

from app.connectors.base import AclMappingContext

from .subject import REQUIRED_CASE_IDS, AclSubject
from .subjects import SUBJECT_IDS, SUBJECTS

pytestmark = pytest.mark.parametrize("subject", SUBJECTS, ids=SUBJECT_IDS)

# Fixed seed: the property test must be reproducible, not flaky-by-design.
_FUZZ_SEED = 0xACE4C4
_FUZZ_ROUNDS = 300


# --- the roster gate ----------------------------------------------------------


@pytest.mark.parametrize("case_id", REQUIRED_CASE_IDS)
def test_subject_supplies_every_required_case(subject: AclSubject, case_id: str) -> None:
    """Every ADR-0019 §2 deny rule has a fixture for this connector.

    The mechanism behind "adding a connector means adding a fixture": a subject
    that cannot express one of these rules in its own vocabulary fails here.
    """
    case = subject.case(case_id)
    assert case.why, f"{subject.name}:{case_id} must say WHY it admits/denies"


# --- the effective-read fixtures ---------------------------------------------


def test_every_case_maps_exactly_as_declared(subject: AclSubject) -> None:
    """Each fixture's mapped set equals its declared set — no more, no less."""
    for case in subject.cases:
        got = subject.map_acl(case.raw, subject.context)
        assert got == case.admits, f"{subject.name}:{case.id} ({case.why})"


def test_deny_rules_grant_nobody(subject: AclSubject) -> None:
    """The ADR's deny vocabulary maps to the EMPTY set, connector-agnostically."""
    deny_rules = (
        "empty_acl",
        "group_only",
        "domain_only",
        "unattested_user",
        "revoked_entry",
        "expiring_entry",
        "metadata_only_entry",
        "unknown_role",
        "unknown_type",
        "inherited_under_reduction",
    )
    for case_id in deny_rules:
        assert subject.mapped(case_id) == frozenset(), f"{subject.name}:{case_id} must deny"


def test_limited_access_admits_only_direct_principals(subject: AclSubject) -> None:
    """A limited-access item inside a shared container admits its direct set only."""
    case = subject.case("limited_access_direct_only")
    got = subject.map_acl(case.raw, subject.context)
    assert got == frozenset({f"user:{subject.attested_user_id}"})
    assert got == case.admits


def test_domain_share_never_yields_the_tenant_principal(subject: AclSubject) -> None:
    """The guest proof at the mapping layer (ADR-0019 §2).

    A domain-restricted share must not become the tenant-wide principal: the
    tenant contains a guest whose email domain is outside the sharing domain,
    and the source would deny them. The store-level half of this proof (that
    guest retrieving nothing) lives in ``test_stores``.
    """
    assert subject.guest_email.rsplit("@", 1)[-1] != subject.sharing_domain
    assert "tenant" not in subject.mapped("domain_only")
    assert subject.mapped("domain_only") == frozenset()


def test_an_empty_identity_snapshot_admits_the_public_share_and_nobody_else(
    subject: AclSubject,
) -> None:
    """A tenant with **no attested users** still resolves the safe wildcard.

    ``anyone`` needs no identity map (every tenant member is a strict subset of
    "everyone"), while every user-matched entry maps to nothing. Restores the
    coverage of the deleted ``test_empty_context_still_maps_anyone_but_no_users``
    (#453) in connector-agnostic form.
    """
    empty = AclMappingContext(email_to_user_id={}, evaluated_at=subject.context.evaluated_at)
    assert subject.map_acl(subject.case("public").raw, empty) == frozenset({"tenant"})
    assert subject.map_acl(subject.case("direct_user").raw, empty) == frozenset()


def test_unattested_email_lights_up_once_attested(subject: AclSubject) -> None:
    """Attestation is the ONLY thing that changes for an unattested match.

    Same raw payload, same mapper: nothing while the email carries no identity
    attestation, the user principal the moment the snapshot contains it — which
    is why "attestation lights it up at the next sync" is a mapping property,
    not a re-fetch.
    """
    raw = subject.single_user_acl(subject.unattested_email)
    assert subject.map_acl(raw, subject.context) == frozenset()

    attested_now = subject.tenant_users[subject.unattested_email]
    widened = type(subject.context)(
        email_to_user_id={
            **subject.context.email_to_user_id,
            subject.unattested_email: attested_now,
        },
        evaluated_at=subject.context.evaluated_at,
    )
    assert subject.map_acl(raw, widened) == frozenset({f"user:{attested_now}"})


# --- never-escalate ----------------------------------------------------------


def _fuzz(subject: AclSubject) -> list[Mapping[str, object]]:
    rng = random.Random(_FUZZ_SEED)
    return [subject.generate(rng) for _ in range(_FUZZ_ROUNDS)]


def test_mapped_set_is_a_subset_of_the_source_allow_list(subject: AclSubject) -> None:
    """The never-escalate property (spec 0004 §2.2), over generated fixtures.

    ``source_admits`` is an independent, deliberately *permissive* model of the
    source's own behaviour, so this can only fail by the mapper WIDENING: a
    domain share becoming tenant-wide, a revoked/expired/metadata-only entry
    being admitted, or an inherited entry surviving the reduction.
    """
    for raw in _fuzz(subject):
        mapped = subject.map_acl(raw, subject.context)
        source = subject.source_admits(raw)
        assert mapped <= source, f"{subject.name}: mapper widened {mapped - source} for {raw!r}"


def test_declared_cases_also_satisfy_never_escalate(subject: AclSubject) -> None:
    """The hand-written fixtures obey the same subset law as the generated ones."""
    for case in subject.cases:
        mapped = subject.map_acl(case.raw, subject.context)
        assert mapped <= subject.source_admits(case.raw), f"{subject.name}:{case.id}"


def test_mapping_is_deterministic_and_pure(subject: AclSubject) -> None:
    """Same payload, same snapshot ⇒ same set (no clock, no I/O, no module state)."""
    for raw in _fuzz(subject)[:50]:
        assert subject.map_acl(raw, subject.context) == subject.map_acl(raw, subject.context)


def test_dropping_an_entry_never_adds_a_principal(subject: AclSubject) -> None:
    """Monotonicity: a narrower source ACL can never map to a wider Lumen set."""
    for raw in _fuzz(subject)[:80]:
        entries_key = next((k for k in ("permissions", "grants") if k in raw), None)
        entries = raw.get(entries_key) if entries_key else None
        if not isinstance(entries, list) or not entries:
            continue
        full = subject.map_acl(raw, subject.context)
        for index in range(len(entries)):
            narrowed = {**raw, entries_key: entries[:index] + entries[index + 1 :]}
            assert subject.map_acl(narrowed, subject.context) <= full


def test_mapped_principals_use_the_declared_vocabulary(subject: AclSubject) -> None:
    """Only ``user:<uuid>`` / ``tenant`` are ever emitted (ADR-0019 §2 v1)."""
    for raw in _fuzz(subject):
        for principal in subject.map_acl(raw, subject.context):
            assert principal in subject.universe(), f"{subject.name}: alien principal {principal}"


# --- meta: the never-escalate proof is not vacuous ----------------------------


def test_the_never_escalate_property_detects_a_widening_mapper(subject: AclSubject) -> None:
    """A ⊆ proof against an all-admitting oracle would pass for any mapper.

    Runs the property against a deliberately broken mapper (one that always
    adds the tenant-wide principal) and asserts the oracle rejects it — so the
    green result above means "the mapper does not widen", not "the oracle is
    permissive".
    """
    escalations = [
        raw
        for raw in _fuzz(subject)
        if not (subject.map_acl(raw, subject.context) | {"tenant"}) <= subject.source_admits(raw)
    ]
    assert escalations, f"{subject.name}: the oracle admits everything — the ⊆ proof is vacuous"


def test_the_oracle_refuses_a_domain_to_tenant_mapping(subject: AclSubject) -> None:
    """The specific widening the ADR forbids is the one the oracle rejects."""
    domain_only = subject.case("domain_only").raw
    assert "tenant" not in subject.source_admits(domain_only)
