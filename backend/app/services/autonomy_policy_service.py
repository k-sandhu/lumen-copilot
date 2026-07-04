"""Admin autonomy-governance use-cases (issue #218) — the per-tenant autonomy cap.

The orchestration behind the ``/admin/autonomy-policy`` GET/PATCH surface (ADR-0004:
``services/`` composes adapters; the router calls exactly one service). It reads and
writes the per-tenant **autonomy cap** — the maximum
:class:`~app.domain.entities.AutonomyLevel` any assistant in the tenant may
effectively run at (ADR-0011 §3). This is the ceiling the publish path and the
run-time tool gate consult: an assistant's EFFECTIVE autonomy is
``min(assistant.autonomy_level, tenant cap)``.

**Permissive-by-default.** Autonomy governance is a *ceiling*, not a switch — the
absence of a cap means "no ceiling" (an assistant runs at its own configured level),
mirroring how ``tool_policy`` / ``sandbox_policy`` fall back to their built-in
defaults. A stored cap only ever *narrows*: it can lower an assistant's effective
autonomy, never raise it. (Deny-by-default still governs the tools themselves — a T1
side-effecting tool is denied unless the *effective* autonomy is high enough; the cap
can only make that gate stricter.)

Role gating (admin-only, INV-5) is enforced one layer up by the ``/admin`` router's
``require_roles(Role.ADMIN)`` dependency; a member/non-admin never reaches this
service (403). Tenant binding (spec 0004 §2.3): the tenant id is the one the router
resolved from the caller's token, never request input (INV-1). The write is a
reversible, tenant-scoped **T1** governance action, audited here
(``autonomy_cap.updated``, INV-6). An unknown autonomy value never reaches the
service (the wire model constrains it to the enum); the DB CHECK is the last backstop.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import AuditEventRepository, TenantAutonomyPolicyRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, AutonomyLevel
from app.services.audit import AuditSink

# The default ceiling reported when no per-tenant cap is stored: the highest autonomy
# (``ACT_AUTO``). "No cap" means "no ceiling", so an assistant runs at its own level —
# equivalently, the effective ceiling is the top of the scale.
_NO_CAP_DEFAULT: AutonomyLevel = AutonomyLevel.ACT_AUTO


@dataclass(frozen=True, slots=True)
class AutonomyPolicyView:
    """The EFFECTIVE per-tenant autonomy cap for the admin console (issue #218).

    ``max_autonomy`` is the ceiling an assistant's effective autonomy is ``min``'d to.
    ``is_default`` is True when no per-tenant cap is stored — there is no ceiling, so
    the reported ``max_autonomy`` is the top of the scale (``ACT_AUTO``): every
    assistant runs at its own configured level. ``levels`` lists the four autonomy
    values in ascending order so the console can render the choice set without
    hard-coding it.
    """

    max_autonomy: AutonomyLevel
    is_default: bool
    levels: tuple[AutonomyLevel, ...]


def _view(cap: AutonomyLevel | None) -> AutonomyPolicyView:
    """Project a stored cap (or its absence) into the effective admin view.

    No row ⇒ no ceiling: ``max_autonomy`` reported as ``ACT_AUTO``, ``is_default=True``.
    A stored row reports its ceiling with ``is_default=False``.
    """
    return AutonomyPolicyView(
        max_autonomy=cap if cap is not None else _NO_CAP_DEFAULT,
        is_default=cap is None,
        levels=tuple(AutonomyLevel),
    )


class AutonomyPolicyService:
    """Read/write the per-tenant autonomy cap for one tenant (issue #218).

    Constructed per-request with the session and the resolved ``tenant_id`` (from the
    token, never request input). Role gating (admin-only, INV-5) is the ``/admin``
    router's ``require_roles`` dependency; this service is only reached once that gate
    has passed. The read projects the stored cap (or its absence) into the effective
    view; the write upserts the singleton and audits ``autonomy_cap.updated``.
    """

    def __init__(self, session: AsyncSession, *, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._policies = TenantAutonomyPolicyRepository(session, tenant_id)

    async def get_policy(self) -> AutonomyPolicyView:
        """The tenant's effective autonomy cap.

        No stored row ⇒ no ceiling (``max_autonomy=act_auto``, ``is_default=True``).
        Tenant-scoped (INV-1).
        """
        cap = await self._policies.get()
        return _view(cap.max_autonomy if cap is not None else None)

    async def set_policy(
        self,
        *,
        max_autonomy: AutonomyLevel,
        actor_id: UUID,
        request_id: str,
        source_ip: str,
    ) -> AutonomyPolicyView:
        """Set the tenant's autonomy cap (issue #218) — a reversible T1 write.

        Upserts the per-tenant singleton to ``max_autonomy`` and audits
        ``autonomy_cap.updated`` (INV-6). Returns the effective view (a read-back).
        The new ceiling takes effect immediately: subsequent publishes above it are
        rejected, and running assistants above it are clamped at the run-time tool gate.
        The wire model constrains ``max_autonomy`` to the enum, so no invalid value can
        reach here.
        """
        stored = await self._policies.upsert(
            max_autonomy=max_autonomy, updated_by=actor_id
        )
        # Audit the governance write (INV-6 / mission filter #4) — who set the tenant's
        # autonomy ceiling to what, so the cap the publish path and the run-time gate
        # consult is attributable after the fact.
        audit = AuditSink(AuditEventRepository(self._session, self._tenant_id))
        await audit.emit(
            action=AuditAction.AUTONOMY_CAP_UPDATED,
            actor=AuditActor.user(actor_id),
            resource_type="tenant",
            resource_id=str(self._tenant_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=request_id,
            source_ip=source_ip,
            metadata={"max_autonomy": stored.max_autonomy.value},
        )
        return _view(stored.max_autonomy)


class AutonomyPolicyReader:
    """Resolve the effective autonomy cap the publish + run paths consult (issue #218).

    Constructed with a DB session and the running tenant id (from the principal, never
    request input). Unlike the sandbox/tool readers this is **fail-open** for the cap
    itself: a cap is a *ceiling* over the tools' own deny-by-default gate, so if the cap
    row cannot be read the assistant falls back to its own configured autonomy — which
    is still subject to the per-tool deny-by-default gate. It never *raises* an
    assistant above its own level; the worst case of an unreadable cap is that a ceiling
    is momentarily not applied, never that a tool executes that its own autonomy would
    not already permit.
    """

    def __init__(self, session: AsyncSession, *, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def effective_cap(self) -> AutonomyLevel:
        """The tenant's autonomy ceiling, or ``ACT_AUTO`` (no ceiling) if none is stored.

        Tenant-scoped (INV-1). Any read error resolves to ``ACT_AUTO`` (no additional
        ceiling) — the cap only ever narrows, so a missing ceiling can never widen an
        assistant past its own configured level.
        """
        try:
            cap = await TenantAutonomyPolicyRepository(
                self._session, self._tenant_id
            ).get()
        except Exception:  # noqa: BLE001 — an unreadable cap must not raise the effective level
            return _NO_CAP_DEFAULT
        return cap.max_autonomy if cap is not None else _NO_CAP_DEFAULT

    async def clamp(self, level: AutonomyLevel) -> AutonomyLevel:
        """``level`` lowered to the tenant cap if it exceeds it (the effective autonomy).

        The one call the publish path and the run assembler use: EFFECTIVE autonomy =
        ``min(level, tenant cap)``. Tenant-scoped (INV-1).
        """
        return level.clamped_to(await self.effective_cap())


__all__ = [
    "AutonomyPolicyReader",
    "AutonomyPolicyService",
    "AutonomyPolicyView",
]
