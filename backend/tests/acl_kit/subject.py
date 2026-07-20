"""The connector-agnostic contract every ACL-declaring connector satisfies.

An :class:`AclSubject` is everything the kit needs to prove ADR-0019 §2 for one
connector. It is deliberately small — a mapper, a frozen identity snapshot, a
named fixture per deny rule, an independent "what the source would allow"
oracle, and a fuzz generator — so that onboarding the *next* managed connector
is a fixture, not a test file.

Four rules keep the kit honest as connectors are added:

* :data:`REQUIRED_CASE_IDS` is the ADR-0019 §2 deny vocabulary. A subject that
  omits one fails :mod:`tests.acl_kit.test_mapping` — the effective-read table
  cannot be silently narrowed for a new source.
* :data:`REQUIRED_CASCADE_PROBE_IDS` is the §3 unprovable-set vocabulary. A
  probe *induces* a real failure condition in the connector and returns the
  pages it emitted, so the kit proves **cause → ``integrity=incomplete``**
  rather than assuming a connector produces the signal it is handed.
* **the mapper under test is the LIVE one.** :attr:`AclSubject.map_acl` is a
  property resolving ``get_map_acl(get_connector(name))`` for a *registered*
  connector, so every proof runs through the production wrapper — not through a
  helper a subject happened to import. :attr:`AclSubject.declared_map_acl`
  records what the subject *claims* the mapper is, and ``test_roster`` asserts
  the two agree over every declared and generated payload. Without this a broken
  wrapper delegating to a healthy helper would leave the whole kit green.
* the never-escalate oracle (:attr:`AclSubject.source_admits`) is written from
  the **source's** semantics, independently of ``map_acl``, and is deliberately
  a *superset* wherever the source's behaviour is unknowable from the fixture
  (unknown roles, unprovable inheritance). A superset oracle keeps ``mapped ⊆
  source`` meaningful: it can only ever fail because the mapper widened.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from app.connectors.base import AclMappingContext, SyncPage, get_map_acl
from app.connectors.registry import get_connector, registered_types

AclMapper = Callable[[Mapping[str, object], AclMappingContext], frozenset[str]]


def registry_mapper(name: str) -> AclMapper | None:
    """The LIVE ``map_acl`` of a registered connector, or ``None``.

    Resolved through the same ``get_map_acl`` probe the framework uses to derive
    the ``acl_enforced`` write mode, so "the mapper the kit exercises" and "the
    mapper production calls" are the same object by construction.
    """
    if name not in registered_types():
        return None
    return get_map_acl(get_connector(name))


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
    # An unknown/ABSENT field state: a payload that omits the ACL field entirely,
    # as distinct from carrying an empty one. A mapper that defaults a missing
    # field to the tenant principal fails here and nowhere else.
    "missing_acl_payload",
)

# The ADR-0019 §3 unprovable-affected-set vocabulary. Each id names a *cause*
# the connector must turn into ``integrity=incomplete``. The framework's reaction
# to that signal is a single contract, so a cause that never emits it is
# invisible downstream — which is exactly what these probes exist to catch.
REQUIRED_CASCADE_PROBE_IDS: tuple[str, ...] = (
    "enumeration_failure",  # the descendant walk itself failed
    "budget_exhausted",  # more affected descendants than one run may re-examine
    "healthy_cascade",  # the control (below)
)

# The subset that MUST produce ``integrity=incomplete``. ``healthy_cascade`` is
# deliberately excluded: it is the control proving a connector is not passing
# these probes by reporting INCOMPLETE unconditionally, which would be
# fail-closed but useless.
UNPROVABLE_CAUSE_IDS: tuple[str, ...] = ("enumeration_failure", "budget_exhausted")


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
class CascadeProbe:
    """How to induce one §3 unprovable-affected-set condition in a connector.

    ``induce`` drives the connector's **real** change-replay capability under
    that condition and returns the pages it emitted; the kit then asserts the
    connector itself raised ``integrity=incomplete``. Handing the framework a
    pre-formed incomplete page would only re-prove the framework's reaction —
    the cause→signal link is the part that can silently rot.
    """

    id: str
    why: str
    induce: Callable[[], Awaitable[Sequence[SyncPage]]]


@dataclass(frozen=True)
class AclSubject:
    """One connector under the kit (``gdrive`` today; the fake proves generality).

    Attributes:
        name: the registry key (``sources.type``) this subject stands for.
        declared_map_acl: what the subject claims the mapper is. Proofs do NOT
            call this — they call :attr:`map_acl`, which resolves the live
            registry mapper for a registered connector; ``test_roster`` pins the
            two to be equivalent.
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
        cascade_probes: one :class:`CascadeProbe` per
            :data:`REQUIRED_CASCADE_PROBE_IDS`, each inducing a real failure in
            this connector's change-replay capability.
    """

    name: str
    declared_map_acl: AclMapper
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
    cascade_probes: Mapping[str, CascadeProbe] = field(default_factory=dict)

    @property
    def map_acl(self) -> AclMapper:
        """The mapper every proof runs through — LIVE for a registered connector.

        A subject cannot accidentally exercise a safe helper while the connector
        wrapper is broken: for anything in the registry this resolves the wrapper
        itself. The unregistered synthetic subject (test-only by design) falls
        back to its declared mapper.
        """
        live = registry_mapper(self.name)
        return live if live is not None else self.declared_map_acl

    @property
    def is_registered(self) -> bool:
        """True when this subject stands for a real, discoverable connector."""
        return self.name in registered_types()

    def probe(self, probe_id: str) -> CascadeProbe:
        """The cascade probe registered under ``probe_id``."""
        try:
            return self.cascade_probes[probe_id]
        except KeyError as exc:
            raise AssertionError(
                f"{self.name}: no cascade probe {probe_id!r} — see REQUIRED_CASCADE_PROBE_IDS"
            ) from exc

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


__all__ = [
    "REQUIRED_CASCADE_PROBE_IDS",
    "REQUIRED_CASE_IDS",
    "UNPROVABLE_CAUSE_IDS",
    "AclCase",
    "AclMapper",
    "AclSubject",
    "CascadeProbe",
    "registry_mapper",
]
