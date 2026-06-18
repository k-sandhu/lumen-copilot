"""Audit taxonomy + envelope policy — pure domain (spec 0004 §2.4, issue #23).

The *policy* half of the product audit log (mission filter #4, "auditable"):
the event-type taxonomy, the actor value object, and the fail-closed
required-field gate. Pure — **no ORM, no SQLAlchemy, no framework imports**
(backend/AGENTS.md: ``domain/`` is pure). The I/O half (writing through the
append-only ``AuditEventRepository``) lives in ``app.services.audit``.

The audit log is a **product** record, distinct from ops telemetry
(structlog/OTel/Prometheus, ``app.core.logging``) — ADR-0004 "audit through one
sink". Every consequential read/answer/auth/denied-access emits one event whose
shape is pinned here so the sink can never persist a malformed record (INV-6).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from uuid import UUID

from app.domain.entities import AuditOutcome


class AuditAction(str, enum.Enum):
    """The audit event taxonomy (spec 0004 §2.4) — the *only* permitted actions.

    Deny by default: an action outside this set is rejected by
    ``validate_envelope`` before any write. The ``action.*`` trio is **reserved**
    for the write tiers (T2+, spec 0004 §2.5) — present so the taxonomy is stable
    when the approval-gated action path lands, but unused at the (all-T0) MVP.
    """

    AUTH_LOGIN = "auth.login"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    AUTH_LOGOUT = "auth.logout"
    COLLECTION_CREATED = "collection.created"
    DOCUMENT_UPLOADED = "document.uploaded"
    DOCUMENT_VIEWED = "document.viewed"
    DOCUMENT_DOWNLOADED = "document.downloaded"
    DOCUMENT_DELETED = "document.deleted"
    RETRIEVAL_QUERY = "retrieval.query"
    ANSWER_GENERATED = "answer.generated"
    PERMISSION_DENIED = "permission.denied"
    # Reserved for the write tiers (T2+) — see spec 0004 §2.5.
    ACTION_REQUESTED = "action.requested"
    ACTION_APPROVED = "action.approved"
    ACTION_EXECUTED = "action.executed"


# Convenience membership set for cheap "is this a known action?" checks without
# constructing the enum. Always mirrors ``AuditAction`` (asserted in tests).
AUDIT_ACTIONS: frozenset[str] = frozenset(a.value for a in AuditAction)


class AuditEnvelopeError(ValueError):
    """A proposed audit event is malformed — a required field is missing or invalid.

    Raised by ``validate_envelope`` (and therefore by the sink's ``emit``)
    *before* anything is written, so a defective event never reaches the
    append-only table (INV-6, fail-closed). It is a domain error, not an HTTP
    concern; the calling feature decides how (if at all) to surface it — but a
    failure to audit must never silently succeed.
    """


@dataclass(frozen=True, slots=True)
class AuditActor:
    """Who performed an audited action (spec 0004 §2.4 ``actor_id`` field).

    Exactly one of three shapes: an authenticated **user** (carries a user id),
    the **system** (background tasks, scheduled jobs — no user id), or
    **anonymous** (an unauthenticated principal, e.g. a failed login — no user
    id). The two id-less shapes are distinguished by ``is_system`` /
    ``is_anonymous`` so a reader can tell "the platform did this" from "someone
    not yet identified did this" even though both store ``actor_id = NULL``.
    """

    actor_id: UUID | None
    is_system: bool = False
    is_anonymous: bool = False

    @classmethod
    def user(cls, user_id: UUID) -> AuditActor:
        """An authenticated principal, identified by ``user_id``."""
        return cls(actor_id=user_id)

    @classmethod
    def system(cls) -> AuditActor:
        """The platform itself (a task/job with no human actor)."""
        return cls(actor_id=None, is_system=True)

    @classmethod
    def anonymous(cls) -> AuditActor:
        """An unauthenticated principal (e.g. a failed login attempt)."""
        return cls(actor_id=None, is_anonymous=True)

    @property
    def label(self) -> str:
        """A stable human label: the user id, ``"system"``, or ``"anonymous"``."""
        if self.is_system:
            return "system"
        if self.is_anonymous:
            return "anonymous"
        return str(self.actor_id)


def _coerce_action(action: AuditAction | str) -> AuditAction:
    """Normalise an action to a taxonomy member, or raise.

    Accepts the enum directly, or a taxonomy string (so call sites need not
    import the enum). Anything outside the taxonomy is a deny-by-default error.
    """
    if isinstance(action, AuditAction):
        return action
    try:
        return AuditAction(action)
    except ValueError as exc:
        raise AuditEnvelopeError(
            f"action {action!r} is not in the audit taxonomy (spec 0004 §2.4)"
        ) from exc


def validate_envelope(
    *,
    tenant_id: UUID | None,
    action: AuditAction | str | None,
    resource_type: str | None,
    outcome: AuditOutcome | None,
    resource_id: str | None,
    request_id: str | None,
    source_ip: str | None,
) -> AuditAction:
    """Fail-closed required-field gate for an audit event (INV-6).

    Validates the always-required envelope fields *before* a write and returns
    the normalised :class:`AuditAction`. ``event_id`` and ``ts`` are
    server-assigned (uuid default + ``now()``), and ``actor_id`` is allowed to
    be null (system/anonymous), so they are not checked here; everything else
    listed under spec 0004 §2.4 "Required fields (every event)" must be present
    and well-formed — including ``resource_id``, ``request_id``, and
    ``source_ip`` (the spec outranks code, AGENTS.md §4): an event with any of
    them nulled or blank is rejected, never persisted.

    Raises:
        AuditEnvelopeError: if any required field is missing or invalid. Nothing
            is persisted in that case — the caller's ``emit`` aborts before the
            repository is touched.
    """
    if tenant_id is None:
        raise AuditEnvelopeError("audit event requires a tenant_id")
    if action is None:
        raise AuditEnvelopeError("audit event requires an action")
    if resource_type is None or not str(resource_type).strip():
        raise AuditEnvelopeError("audit event requires a non-empty resource_type")
    if outcome is None:
        raise AuditEnvelopeError("audit event requires an outcome")
    if not isinstance(outcome, AuditOutcome):
        raise AuditEnvelopeError(
            f"outcome {outcome!r} is not a valid AuditOutcome (allowed|denied|error)"
        )
    # Per spec 0004 §2.4 these three are "Required fields (every event)"; the
    # spec outranks code (AGENTS.md §4), so enforce them fail-closed rather than
    # silently persisting an event with them nulled.
    if resource_id is None or not str(resource_id).strip():
        raise AuditEnvelopeError("audit event requires a non-empty resource_id")
    if request_id is None or not str(request_id).strip():
        raise AuditEnvelopeError("audit event requires a non-empty request_id")
    if source_ip is None or not str(source_ip).strip():
        raise AuditEnvelopeError("audit event requires a non-empty source_ip")
    return _coerce_action(action)
