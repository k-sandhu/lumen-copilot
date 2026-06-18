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


__all__ = ["AuditAction", "AuditActor", "AuditOutcome", "AuditSink"]
