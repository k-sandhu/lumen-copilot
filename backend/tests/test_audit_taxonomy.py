"""Audit taxonomy + envelope validation — pure domain (issue #23, spec 0004 §2.4).

These exercise the *policy* half of the audit sink with zero I/O: the event-type
taxonomy is exactly the set spec 0004 §2.4 enumerates, the actor value object
models user / system / anonymous principals, and ``validate_envelope`` is the
fail-closed gate that rejects an event missing a required field **before** any
write (the INV-6 negative is asserted at the service layer in
``test_audit_sink.py``; here we pin the rule itself).
"""

from __future__ import annotations

import uuid

import pytest

from app.domain.audit import (
    AUDIT_ACTIONS,
    AuditAction,
    AuditActor,
    AuditEnvelopeError,
    validate_envelope,
)
from app.domain.entities import AuditOutcome


def test_taxonomy_is_exactly_spec_0004_set() -> None:
    """The action enum is the spec 0004 §2.4 taxonomy + the additive extensions.

    Spec 0004 §2.4 pins the MVP taxonomy; later-accepted features extend it
    additively (deny-by-default is preserved — the set only grows, no action is
    relaxed): ADR-0009 §5 (the connector framework) requires an audit event on
    every source add / sync / delete (``source.*``), and CC-1 / issue #18 (explicit
    ACL grants, spec 0004 §2.2) audits every share grant / revoke
    (``permission.granted`` / ``permission.revoked``).
    """
    assert {a.value for a in AuditAction} == {
        "auth.login",
        "auth.login_failed",
        "auth.logout",
        "collection.created",
        "document.uploaded",
        "document.viewed",
        "document.downloaded",
        "document.deleted",
        # Connector sources (ADR-0009 §5) — additive to the spec 0004 MVP set.
        "source.added",
        "source.synced",
        "source.deleted",
        "retrieval.query",
        "answer.generated",
        "permission.denied",
        # Explicit ACL grants (CC-1 / issue #18, spec 0004 §2.2) — additive.
        "permission.granted",
        "permission.revoked",
        # Admin per-tenant settings write (issue #148) — additive; the first
        # /admin write, a reversible T1 action audited like every consequential
        # action (mission filter #4 "auditable").
        "tenant.settings_updated",
        # Agent/run-produced artifacts (issue #208, CC-12) — additive; every
        # create/download/delete of a produced file is audited (a T1 action).
        "artifact.created",
        "artifact.downloaded",
        "artifact.deleted",
        # Reserved for the write tiers (T2+) — present but unused at MVP.
        "action.requested",
        "action.approved",
        "action.executed",
    }


def test_audit_actions_frozenset_mirrors_enum() -> None:
    """The string convenience set mirrors the enum (used for cheap membership)."""
    assert AUDIT_ACTIONS == frozenset(a.value for a in AuditAction)


def test_actor_user_carries_id() -> None:
    uid = uuid.uuid4()
    actor = AuditActor.user(uid)
    assert actor.actor_id == uid
    assert not actor.is_system
    assert not actor.is_anonymous
    assert actor.label == str(uid)


def test_actor_system_has_no_id() -> None:
    actor = AuditActor.system()
    assert actor.actor_id is None
    assert actor.is_system
    assert actor.label == "system"


def test_actor_anonymous_has_no_id() -> None:
    actor = AuditActor.anonymous()
    assert actor.actor_id is None
    assert actor.is_anonymous
    assert actor.label == "anonymous"


def _valid_kwargs() -> dict[str, object]:
    return {
        "tenant_id": uuid.uuid4(),
        "action": AuditAction.RETRIEVAL_QUERY,
        "resource_type": "query",
        "outcome": AuditOutcome.ALLOWED,
        "resource_id": str(uuid.uuid4()),
        "request_id": "req-abc",
        "source_ip": "203.0.113.7",
    }


def test_validate_envelope_accepts_a_complete_event() -> None:
    """A complete envelope passes the gate (returns the action, raises nothing)."""
    validate_envelope(**_valid_kwargs())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "missing",
    [
        "tenant_id",
        "action",
        "resource_type",
        "outcome",
        # Spec 0004 §2.4 "Required fields (every event)" — enforced fail-closed.
        "resource_id",
        "request_id",
        "source_ip",
    ],
)
def test_validate_envelope_rejects_a_missing_required_field(missing: str) -> None:
    """INV-6 (policy): a missing required field is rejected *before* any write.

    Covers every spec 0004 §2.4 required field validated here — including
    ``resource_id``/``request_id``/``source_ip`` (the spec outranks code,
    AGENTS.md §4).
    """
    kwargs = _valid_kwargs()
    kwargs[missing] = None
    with pytest.raises(AuditEnvelopeError) as excinfo:
        validate_envelope(**kwargs)  # type: ignore[arg-type]
    assert missing in str(excinfo.value)


@pytest.mark.parametrize("blank_field", ["resource_type", "resource_id", "request_id", "source_ip"])
def test_validate_envelope_rejects_a_blank_required_string(blank_field: str) -> None:
    """A whitespace-only required string is as good as missing (fail-closed)."""
    kwargs = _valid_kwargs()
    kwargs[blank_field] = "   "
    with pytest.raises(AuditEnvelopeError) as excinfo:
        validate_envelope(**kwargs)  # type: ignore[arg-type]
    assert blank_field in str(excinfo.value)


def test_validate_envelope_rejects_an_unknown_action_string() -> None:
    """A free-text action outside the taxonomy is rejected (deny by default)."""
    kwargs = _valid_kwargs()
    kwargs["action"] = "totally.made.up"
    with pytest.raises(AuditEnvelopeError):
        validate_envelope(**kwargs)  # type: ignore[arg-type]


def test_validate_envelope_accepts_action_as_string_in_taxonomy() -> None:
    """A taxonomy string (not the enum) is accepted and normalised."""
    kwargs = _valid_kwargs()
    kwargs["action"] = "answer.generated"
    validate_envelope(**kwargs)  # type: ignore[arg-type]
