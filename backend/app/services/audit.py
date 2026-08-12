"""Audit sink service — the one injectable ``emit(...)`` (spec 0004 §2.4, issue #23).

The single product-audit sink (ADR-0004 "audit through one sink"). Every later
feature that does a consequential read/answer/auth/denied-access calls
``AuditSink.emit(...)`` to satisfy the "auditable" mission filter (§2 #4); there
is exactly one write path, so the audit guarantee is mechanical, not per-feature
discipline.

This composes the two halves: the pure taxonomy/envelope policy
(:mod:`app.domain.audit`) validates the event fail-closed (INV-6) *before* it
reaches the append-only :class:`~app.db.repositories.AuditEventRepository`
(``db/`` — reused from #44, not duplicated). The sink is strictly append-only:
it exposes ``emit`` and nothing else — no update, no delete — and the underlying
table denies UPDATE/DELETE at the DB role (the #44 migration).

Distinct from ops logging (``app.core.logging`` / structlog): that is telemetry
for operators; this is a durable product record queryable by the SEC persona.
The two never share a path.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.audit_transactions import DurableAuditTransactions
from app.db.repositories import AuditEventRepository
from app.domain.audit import AuditAction, AuditActor, validate_envelope
from app.domain.entities import AuditEvent, AuditOutcome


class AuditSink:
    """The injectable product-audit emitter (the "one sink").

    Constructed with a tenant-scoped :class:`AuditEventRepository` (the tenant
    comes from ``auth/`` upstream, never from request input — spec 0004 §2.1).
    A feature obtains a sink via the FastAPI dependency in ``app.api.deps`` and
    calls :meth:`emit`; it never touches the repository or the ORM directly.
    """

    def __init__(self, repository: AuditEventRepository) -> None:
        self._repository = repository

    async def emit(
        self,
        *,
        action: AuditAction | str,
        actor: AuditActor,
        resource_type: str,
        outcome: AuditOutcome,
        resource_id: str,
        request_id: str,
        source_ip: str,
        metadata: dict[str, object] | None = None,
    ) -> AuditEvent:
        """Validate and persist one audit event, returning the stored record.

        The envelope is validated **before** the write (INV-6, fail-closed): a
        missing/invalid required field raises
        :class:`~app.domain.audit.AuditEnvelopeError` and nothing is persisted.
        Per spec 0004 §2.4 "Required fields (every event)", ``resource_id``,
        ``request_id``, and ``source_ip`` are **required** (no silent ``None``
        default) — the spec outranks code (AGENTS.md §4). The repository assigns
        ``event_id`` (uuid) and ``ts`` (UTC ``now()``); the tenant is fixed by
        the repository's scope. The caller owns the transaction boundary (the
        row is flushed, not committed) so the audit write commits atomically
        with the action it records.

        Args:
            action: A taxonomy action (enum or string in the taxonomy).
            actor: Who performed it — user / system / anonymous.
            resource_type: The kind of resource acted on (e.g. ``"document"``).
            outcome: ``allowed`` | ``denied`` | ``error``.
            resource_id: The specific resource id acted on (required).
            request_id: Correlation id, matches the ops-log request id (required).
            source_ip: Client IP the action originated from (required).
            metadata: Event-specific extras (e.g. query hash, retrieved document
                ids, model id, citation count for retrieval/answer events).

        Returns:
            The persisted :class:`AuditEvent` domain entity.

        Raises:
            AuditEnvelopeError: a required field is missing or invalid; no write
                occurs.
        """
        # Fail-closed gate first — never reach the table with a bad envelope.
        validated_action = validate_envelope(
            tenant_id=self._repository.tenant_id,
            action=action,
            resource_type=resource_type,
            outcome=outcome,
            resource_id=resource_id,
            request_id=request_id,
            source_ip=source_ip,
        )
        return await self._repository.record(
            action=validated_action.value,
            resource_type=resource_type,
            outcome=outcome,
            actor_id=actor.actor_id,
            resource_id=resource_id,
            request_id=request_id,
            source_ip=source_ip,
            metadata=metadata,
        )


async def emit_permission_denied(
    audit: AuditSink,
    *,
    actor_id: UUID,
    resource_type: str,
    resource_id: str,
    attempted_action: str,
    reason: str,
    request_id: str,
    source_ip: str,
    required_roles: Sequence[str] = (),
) -> AuditEvent:
    """Emit one safe, authenticated denial through the canonical sink.

    This is the shared INV-6 boundary for authenticated 403/404 decisions. Its
    metadata surface is intentionally closed: callers can record only the
    server-chosen attempted action, a stable reason code, and (for RBAC gates)
    the required role names. Request bodies, content, secrets, and raw provider
    errors have no parameter through which to enter the product audit log.

    This low-level helper only appends to the supplied sink. Authenticated guard
    paths use :class:`PermissionDeniedRecorder` below, which gives the denial a
    deliberately independent transaction so an exception cannot roll it back
    and persisting it cannot commit unrelated caller work.
    """
    metadata: dict[str, object] = {
        "attempted_action": attempted_action,
        "reason": reason,
    }
    if required_roles:
        metadata["required_roles"] = list(required_roles)
    return await audit.emit(
        action=AuditAction.PERMISSION_DENIED,
        actor=AuditActor.user(actor_id),
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=AuditOutcome.DENIED,
        request_id=request_id,
        source_ip=source_ip,
        metadata=metadata,
    )


class PermissionDeniedRecorder:
    """Persist authenticated denials in an isolated tenant-bound transaction.

    Successful action events deliberately share the action transaction so they
    commit or roll back atomically. A denied action has no successful transaction
    to commit, and its 403/404 exception causes the request session to close with
    a rollback. This recorder therefore opens a fresh session from a separately
    owned, bounded audit engine (never the caller's engine/session/connection),
    binds the trusted tenant for Postgres RLS, delegates the append to the canonical
    :class:`AuditSink`, and commits only that audit transaction. Sink, acquisition,
    flush, RLS, and commit failures all propagate; returning an unaudited denial is
    forbidden by INV-6.
    """

    def __init__(
        self,
        transactions: DurableAuditTransactions,
        *,
        tenant_id: UUID,
        request_session: AsyncSession,
    ) -> None:
        # Validation happens at construction, before a service guard can perform
        # an action write.  A caller engine/connection can never be silently
        # reused as the durable boundary (R1-001).
        transactions.assert_independent_from(request_session)
        self._transactions = transactions
        self._tenant_id = tenant_id

    async def emit(
        self,
        *,
        actor_id: UUID,
        resource_type: str,
        resource_id: str,
        attempted_action: str,
        reason: str,
        request_id: str,
        source_ip: str,
        required_roles: Sequence[str] = (),
    ) -> AuditEvent:
        """Append and commit exactly one safe denial, or propagate the failure."""
        async with self._transactions.repository(self._tenant_id) as repository:
            return await emit_permission_denied(
                AuditSink(repository),
                actor_id=actor_id,
                resource_type=resource_type,
                resource_id=resource_id,
                attempted_action=attempted_action,
                reason=reason,
                request_id=request_id,
                source_ip=source_ip,
                required_roles=required_roles,
            )


__all__ = [
    "AuditAction",
    "AuditActor",
    "AuditOutcome",
    "AuditSink",
    "PermissionDeniedRecorder",
    "emit_permission_denied",
]
