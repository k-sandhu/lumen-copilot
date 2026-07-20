"""The connector-agnostic contract every ACL-declaring connector satisfies.

An :class:`AclSubject` is everything the kit needs to prove ADR-0019 §2 for one
connector. It is deliberately small — a mapper, a frozen identity snapshot, a
named fixture per deny rule, an independent "what the source would allow"
oracle, and a fuzz generator — so that onboarding the *next* managed connector
is a fixture, not a test file.

Two rules keep the kit honest as connectors are added:

* :data:`REQUIRED_CASE_IDS` is the ADR-0019 §2 deny vocabulary. A subject that
  omits one fails :mod:`tests.acl_kit.test_mapping` — the effective-read table
  cannot be silently narrowed for a new source.
* the never-escalate oracle (:attr:`AclSubject.source_admits`) is written from
  the **source's** semantics, independently of ``map_acl``, and is deliberately
  a *superset* wherever the source's behaviour is unknowable from the fixture
  (unknown roles, unprovable inheritance). A superset oracle keeps ``mapped ⊆
  source`` meaningful: it can only ever fail because the mapper widened.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from uuid import UUID

from app.connectors.base import AclMappingContext

# The ADR-0019 §2 effective-read vocabulary — one required fixture per rule in
# the negative-test table. Each id names a *behaviour* ("a share that carries an
# expiry"), never a provider field, so a non-Drive connector expresses it in its
# own raw shape.
REQUIRED_CASE_IDS: tuple[str, ...] = (
    # admits
    "direct_user",  # an attested user entry -> user:<uuid>
    "public",  # a public/link-public entry -> the tenant principal
    # denies
    "empty_acl",  # no entries at all -> nobody
    "group_only",  # an unexpanded group -> nobody
    "domain_only",  # a domain-restricted share -> nobody (no verified binding)
    "unattested_user",  # an email with no identity attestation -> nobody
    "revoked_entry",  # a deleted/revoked entry -> nobody
    "expiring_entry",  # ANY expiry present -> nobody (v1 models no time-boxing)
    "metadata_only_entry",  # a metadata-view entry never grants content access
    "unknown_role",  # an unmodelled role -> nobody
    "unknown_type",  # an unmodelled principal type -> nobody
    # inheritance reduction
    "inherited_under_reduction",  # inherited entries ignored when direct-only
    "limited_access_direct_only",  # a limited-access item admits only direct principals
)


@dataclass(frozen=True)
class AclCase:
    """One raw source-ACL fixture and the exact principal set it may yield.

    ``raw`` is in the connector's **own** vocabulary — the kit never inspects
    it, it only feeds it to that connector's ``map_acl``. ``admits`` is the
    complete expected result (``frozenset()`` for every deny rule), so a case is
    an equality assertion, not a "contains" one: a mapper that emits an *extra*
    principal fails just as loudly as one that drops the right one.
    """

    id: str
    raw: Mapping[str, object]
    admits: frozenset[str]
    why: str


@dataclass(frozen=True)
class AclSubject:
    """One connector under the kit (``gdrive`` today; the fake proves generality).

    Attributes:
        name: the registry key (``sources.type``) this subject stands for.
        map_acl: the connector's pure mapper — the real one, never a double.
        context: the frozen attested-identity snapshot ``map_acl`` maps against.
        tenant_users: every Lumen user of the tenant, email -> id, **including
            unattested ones** (the source may well allow them; only Lumen's
            trust basis is missing). The oracle needs the full directory.
        attested_email / attested_user_id: the identity the "admits" cases
            resolve to — the kit builds its store world around this user.
        unattested_email: an existing tenant user with no attestation.
        guest_email: a same-tenant user whose email domain is outside the
            sharing domain of the ``domain_only`` case (the ADR's guest proof).
        sharing_domain: the domain the ``domain_only`` case shares with.
        cases: the effective-read fixtures, one per :data:`REQUIRED_CASE_IDS`
            (extras are welcome and are run too).
        source_admits: the independent never-escalate oracle (see module docs).
        generate: a fuzz generator producing raw payloads for the property test.
    """

    name: str
    map_acl: Callable[[Mapping[str, object], AclMappingContext], frozenset[str]]
    context: AclMappingContext
    tenant_users: Mapping[str, UUID]
    attested_email: str
    attested_user_id: UUID
    unattested_email: str
    guest_email: str
    sharing_domain: str
    cases: tuple[AclCase, ...]
    source_admits: Callable[[Mapping[str, object]], frozenset[str]]
    generate: Callable[[random.Random], Mapping[str, object]]
    # A raw payload naming exactly one email, used by the attestation proof:
    # given an email it must map to ``{user:<id>}`` when that user is attested
    # and to ``frozenset()`` when they are not.
    single_user_acl: Callable[[str], Mapping[str, object]] = field(repr=False, default=lambda _: {})

    def case(self, case_id: str) -> AclCase:
        """The fixture registered under ``case_id`` (KeyError-ish if absent)."""
        for candidate in self.cases:
            if candidate.id == case_id:
                return candidate
        raise AssertionError(f"{self.name}: no ACL case {case_id!r} — see REQUIRED_CASE_IDS")

    def mapped(self, case_id: str) -> frozenset[str]:
        """Run the connector's own mapper over the named fixture."""
        return self.map_acl(self.case(case_id).raw, self.context)

    def universe(self) -> frozenset[str]:
        """Every Lumen principal that could ever name a member of this tenant."""
        return frozenset({"tenant"} | {f"user:{uid}" for uid in self.tenant_users.values()})


__all__ = ["REQUIRED_CASE_IDS", "AclCase", "AclSubject"]
