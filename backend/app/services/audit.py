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

from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from inspect import signature
from typing import Any, ParamSpec, TypeVar
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.core.errors import ForbiddenError, NotFoundError
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

    @property
    def tenant_id(self) -> UUID:
        """Trusted scope for composing tenant-bound accounting services (#690)."""
        return self._repository.tenant_id

    async def emit(
        self,
        *,
        event_id: UUID | None = None,
        action: AuditAction | str,
        actor: AuditActor,
        resource_type: str,
        outcome: AuditOutcome,
        resource_id: str,
        request_id: str,
        source_ip: str,
        metadata: dict[str, object] | None = None,
        durable: bool = False,
    ) -> AuditEvent:
        """Validate and persist one audit event, returning the stored record.

        The envelope is validated **before** the write (INV-6, fail-closed): a
        missing/invalid required field raises
        :class:`~app.domain.audit.AuditEnvelopeError` and nothing is persisted.
        Per spec 0004 §2.4 "Required fields (every event)", ``resource_id``,
        ``request_id``, and ``source_ip`` are **required** (no silent ``None``
        default) — the spec outranks code (AGENTS.md §4). The repository assigns
        ``event_id`` (uuid) and ``ts`` (UTC ``now()``); the tenant is fixed by
        the repository's scope. Ordinary events leave ``event_id`` unset and
        retain fresh append semantics. The durable-denial boundary supplies a
        trusted server-generated identity so an ambiguous COMMIT can be retried
        idempotently; it is never accepted from request input. The caller owns
        the transaction boundary (the row is flushed, not committed) so the
        audit write commits atomically with the action it records.
        With ``durable=True``, the repository opens and commits an independent
        tenant-bound transaction for T2 external intent/results (#518), which
        cannot share the answer transaction or be undone with its rollback.

        Args:
            event_id: Optional trusted server identity for an idempotent durable
                denial. Ordinary callers omit it; request data must never supply it.
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
        # External T2 effects cannot participate in the answer transaction (#518).
        # Their intent/result use independent commits through this same sink.
        record = self._repository.record_committed if durable else self._repository.record
        return await record(
            event_id=event_id,
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
    event_id: UUID,
    actor: AuditActor,
    resource_type: str,
    resource_id: str,
    attempted_action: str,
    reason: str,
    request_id: str,
    source_ip: str,
    required_roles: Sequence[str] = (),
) -> AuditEvent:
    """Emit one safe, trusted-principal denial through the canonical sink.

    This is the shared INV-6 boundary for authenticated 403/404 decisions and
    trusted background guards. Its
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
        event_id=event_id,
        action=AuditAction.PERMISSION_DENIED,
        actor=actor,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=AuditOutcome.DENIED,
        request_id=request_id,
        source_ip=source_ip,
        metadata=metadata,
    )


class PermissionDeniedRecorder:
    """Persist trusted-principal denials in an isolated tenant-bound transaction.

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

    @property
    def tenant_id(self) -> UUID:
        """Trusted tenant scope of the independently owned recorder."""
        return self._tenant_id

    async def emit(
        self,
        *,
        actor: AuditActor,
        resource_type: str,
        resource_id: str,
        attempted_action: str,
        reason: str,
        request_id: str,
        source_ip: str,
        required_roles: Sequence[str] = (),
    ) -> AuditEvent:
        """Append and commit exactly one safe denial, or propagate the failure."""
        # Allocated once per semantic guard invocation, inside this trusted
        # server boundary.  The UUID is reused only by the provider's bounded
        # retry/reconciliation protocol and is never client-controlled.
        event_id = uuid4()

        async def _emit(repository: AuditEventRepository) -> AuditEvent:
            return await emit_permission_denied(
                AuditSink(repository),
                event_id=event_id,
                actor=actor,
                resource_type=resource_type,
                resource_id=resource_id,
                attempted_action=attempted_action,
                reason=reason,
                request_id=request_id,
                source_ip=source_ip,
                required_roles=required_roles,
            )

        return await self._transactions.execute_idempotent(
            self._tenant_id,
            event_id,
            _emit,
        )


class PermissionDeniedContext:
    """Mandatory trusted attribution bundled with the durable denial capability.

    Direct-resource services receive this value as one required constructor
    dependency instead of independently accepting an optional recorder, actor,
    request id, and peer address.  That makes the guard boundary mechanically
    complete: a service that can return a 403/404 cannot be built without the
    canonical durable sink and trusted attribution (R3-002).  User-facing API
    construction binds a token-derived user; trusted background guards may instead
    bind the explicit system or anonymous/null actor shape.  A user service must
    call :meth:`assert_user` or :meth:`require_user`, which prevents those explicit
    non-user shapes (or a foreign user) from being mistaken for its principal.
    """

    def __init__(
        self,
        recorder: PermissionDeniedRecorder,
        *,
        principal: Principal | None = None,
        actor: AuditActor | None = None,
        request_id: str,
        source_ip: str,
    ) -> None:
        if principal is not None:
            if principal.tenant_id != recorder.tenant_id:
                raise ValueError("Denial principal tenant must match the recorder tenant.")
            if actor is not None:
                raise ValueError(
                    "Authenticated denial actor comes only from the trusted principal."
                )
            actor = AuditActor.user(principal.user_id)
        elif actor not in (AuditActor.system(), AuditActor.anonymous()):
            raise ValueError("User denial context requires a trusted tenant-bound principal.")
        assert actor is not None
        if not request_id.strip():
            raise ValueError("Denial context requires a request id.")
        if not source_ip.strip():
            raise ValueError("Denial context requires a source sentinel/address.")
        self._recorder = recorder
        self._actor = actor
        self._request_id = request_id
        self._source_ip = source_ip

    @property
    def actor(self) -> AuditActor:
        """The trusted request/system actor bound at construction."""
        return self._actor

    @property
    def tenant_id(self) -> UUID:
        return self._recorder.tenant_id

    @property
    def request_id(self) -> str:
        """The middleware-minted correlation id."""
        return self._request_id

    @property
    def source_ip(self) -> str:
        """The peer address or explicit system/unknown sentinel."""
        return self._source_ip

    def assert_tenant(self, tenant_id: UUID) -> None:
        if self.tenant_id != tenant_id:
            raise ValueError("Denial context tenant must match the service tenant.")

    def assert_user(self, tenant_id: UUID, user_id: UUID) -> None:
        """Fail before a guard if service identity and audit actor diverge."""
        self.assert_tenant(tenant_id)
        if self.require_user() != user_id:
            raise ValueError("Denial actor must match the service's authenticated principal.")

    def require_user(self) -> UUID:
        """Return the trusted user id, rejecting explicit system/null actor shapes."""
        if self._actor.actor_id is None or self._actor.is_system or self._actor.is_anonymous:
            raise ValueError("Denial context is not bound to an authenticated user.")
        return self._actor.actor_id

    async def emit(
        self,
        *,
        resource_type: str,
        resource_id: str,
        attempted_action: str,
        reason: str,
        required_roles: Sequence[str] = (),
    ) -> AuditEvent:
        """Persist one denial with the bound trusted attribution."""
        # Mark every enclosing operation for this context before writing. A sink
        # failure must propagate, even if it happens to be a typed 403/404; the
        # terminal helper must never mistake it for an unaudited service miss.
        for attempt in _denial_attempts.get():
            # A request builder may construct a separate context for a nested
            # service (chat -> sandbox). Match trusted attribution, not Python
            # object identity, while ContextVar still isolates concurrent tasks.
            if (
                attempt.context.tenant_id == self.tenant_id
                and attempt.context.actor == self.actor
                and attempt.context.request_id == self.request_id
                and attempt.context.source_ip == self.source_ip
            ):
                attempt.recorded = True
        return await self._recorder.emit(
            actor=self._actor,
            resource_type=resource_type,
            resource_id=resource_id,
            attempted_action=attempted_action,
            reason=reason,
            request_id=self._request_id,
            source_ip=self._source_ip,
            required_roles=required_roles,
        )

    async def run_guarded(
        self,
        operation: Callable[[], Awaitable[_Result]],
        *,
        attempted_action: str,
        resource_type: str,
        resource_id: str,
        missing_result: bool,
    ) -> _Result:
        """The single exit for a service operation's terminal missing/denied outcome.

        Wrap the entire use case, not just its first SELECT. Late lookup misses,
        empty RETURNING results and concurrent deletes therefore have identical
        durability. Initial guards retain their more specific safe attribution;
        nested helpers never duplicate their event. Task-local attempt state is
        reset on every exit, including cancellation and audit-write failure.
        """
        attempt = _DenialAttempt(self)
        token = _denial_attempts.set((*_denial_attempts.get(), attempt))
        try:
            try:
                result = await operation()
            except (NotFoundError, ForbiddenError) as error:
                if not attempt.recorded:
                    await self.emit(
                        resource_type=resource_type,
                        resource_id=resource_id,
                        attempted_action=attempted_action,
                        reason="not_visible" if error.status == 404 else "forbidden",
                    )
                raise
            if missing_result and (result is None or result is False) and not attempt.recorded:
                await self.emit(
                    resource_type=resource_type,
                    resource_id=resource_id,
                    attempted_action=attempted_action,
                    reason="not_visible",
                )
            return result
        finally:
            _denial_attempts.reset(token)


_Params = ParamSpec("_Params")
_Result = TypeVar("_Result")


@dataclass
class _DenialAttempt:
    context: PermissionDeniedContext
    recorded: bool = False


_denial_attempts: ContextVar[tuple[_DenialAttempt, ...]] = ContextVar("denial_attempts", default=())


def audited_resource(
    attempted_action: str,
    resource_type: str,
    target_parameter: str | None,
    *,
    missing_result: bool = False,
) -> Callable[
    [Callable[_Params, Awaitable[_Result]]], Callable[_Params, Coroutine[Any, Any, _Result]]
]:
    """Require the trusted context and route the complete service seam through it.

    Only methods whose contract maps None/False to 404 opt into missing_result.
    Successful void deletes and nullable sandbox state remain normal successes.
    Binding uses the original signature, so positional/keyword calls, mandatory
    context introspection, and direct background entry points are preserved.
    The denial manifest's structural regression verifies these actual wrappers.
    """

    def decorate(
        operation: Callable[_Params, Awaitable[_Result]],
    ) -> Callable[_Params, Coroutine[Any, Any, _Result]]:
        parameters = signature(operation)
        if target_parameter is not None and target_parameter not in parameters.parameters:
            raise ValueError(
                f"Unknown denial target {target_parameter} for {operation.__qualname__}"
            )

        @wraps(operation)
        async def guarded(*args: _Params.args, **kwargs: _Params.kwargs) -> _Result:
            bound = parameters.bind(*args, **kwargs)
            bound.apply_defaults()
            context = bound.arguments.get("denials")
            if context is None:
                context = getattr(bound.arguments.get("self"), "_denials", None)
            if not isinstance(context, PermissionDeniedContext):
                raise RuntimeError("Direct-resource operation requires a trusted denial context.")
            target = bound.arguments.get(target_parameter) if target_parameter else None
            return await context.run_guarded(
                lambda: operation(*args, **kwargs),
                attempted_action=attempted_action,
                resource_type=resource_type,
                resource_id=str(target) if target is not None else "new",
                missing_result=missing_result,
            )

        guarded.__denial_action__ = attempted_action  # type: ignore[attr-defined]
        return guarded

    return decorate


__all__ = [
    "AuditAction",
    "AuditActor",
    "AuditOutcome",
    "AuditSink",
    "PermissionDeniedContext",
    "PermissionDeniedRecorder",
    "audited_resource",
    "emit_permission_denied",
]
