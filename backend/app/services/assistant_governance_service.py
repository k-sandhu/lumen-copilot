"""Admin library-governance use-cases for assistants (E6-6/E6-8, issue #217).

The orchestration behind the ``/admin/assistants*`` governance surface (ADR-0004:
``services/`` composes adapters; the router calls exactly one service). It reads
and mutates the **library-governance** axis of the assistants a tenant owns —
certification (certify/deprecate), featuring, disabling, category, and the
accountable ownership (transfer/reassign). This is an *orthogonal* axis to an
assistant's lifecycle ``status`` and its owner-managed CRUD (``AssistantsService``,
#211): the owner builds and publishes; the **admin** governs the library.

**Role gating (INV-5, deny-by-default).** Every method here is admin-only — the
gate is the ``/admin`` router's ``require_roles(Role.ADMIN)`` dependency, so a
member/non-admin never reaches this service (403). Because it is admin-scoped, it
spans **every** owner in the tenant (unlike the owner-scoped ``AssistantsService``,
which 404s a non-owned assistant). It is still tenant-scoped (INV-1): a
cross-tenant assistant id resolves to ``None`` in the repository and is reported
as a **404** (existence non-disclosure) — an admin governs only their own tenant.

**Disabling (INV-8, the load-bearing enforcement).** Disabling an assistant sets
``disabled_at`` **and** flips ``status`` to ``disabled`` in lockstep, so the
existing "only a published assistant may start" gate in ``chat_service`` /
``schedules_service`` / ``runs_service`` refuses a disabled assistant — a disabled
assistant cannot start a chat session, be scheduled, or be run (the mandatory
negative). Re-enabling clears ``disabled_at`` and returns the assistant to
``draft`` (the owner must re-publish to run it again — a disabled assistant is not
silently re-published).

**Ownership (E6-8).** Transferring ownership requires the new owner to be a
distinct member of the tenant (else 422, INV-8); orphaned assistants (owner no
longer a tenant member) are flagged in the list view for admin reassignment, and a
bulk reassign/disable of orphans is offered. Every governance mutation is audited
(INV-6) so a library-governance decision is provable after the fact.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.db.repositories import (
    AssistantRepository,
    AssistantVersionRepository,
    AuditEventRepository,
    UserRepository,
)
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import (
    Assistant,
    AssistantStatus,
    AuditOutcome,
    CertificationState,
)
from app.services.audit import AuditSink, PermissionDeniedRecorder

_MIN_LIMIT = 1
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20

_GOV_CURSOR_PREFIX = "agv:"


def _encode_cursor(row_id: UUID) -> str:
    raw = f"{_GOV_CURSOR_PREFIX}{row_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> UUID:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc
    if not raw.startswith(_GOV_CURSOR_PREFIX):
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor")
    try:
        return UUID(raw[len(_GOV_CURSOR_PREFIX) :])
    except ValueError as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return _DEFAULT_LIMIT
    return max(_MIN_LIMIT, min(_MAX_LIMIT, limit))


@dataclass(frozen=True, slots=True)
class GovernedAssistantPage:
    """One page of governed assistants (newest first) + the opaque next cursor.

    ``bulk_orphan_ids`` is not on the wire; the page carries the projected
    ``owner_orphaned`` flag on each item so the admin UI can surface abandoned
    assistants for reassignment.
    """

    items: list[Assistant]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class BulkOrphanResult:
    """The outcome of a bulk reassign/disable of orphaned assistants (E6-8)."""

    affected: list[UUID]
    action: str


class AssistantGovernanceService:
    """Admin governance of the tenant's assistant library (E6-6/E6-8, issue #217).

    Constructed per-request with the session, the resolved ``tenant_id`` + the
    admin ``actor_id`` (from the token, never request input), and the audit sink +
    correlation context. Role gating (admin-only, INV-5) is the ``/admin`` router's
    ``require_roles`` dependency; this service is only reached once that gate has
    passed. Tenant binding (INV-1) is the repository's; a cross-tenant id is 404.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        denials: PermissionDeniedRecorder,
        request_id: str,
        source_ip: str,
    ) -> None:
        self._session = session
        self._assistants = AssistantRepository(session, tenant_id)
        self._versions = AssistantVersionRepository(session, tenant_id)
        self._users = UserRepository(session, tenant_id)
        self._tenant_id = tenant_id
        self._actor_id = actor_id
        self._audit = AuditSink(AuditEventRepository(session, tenant_id))
        self._denials = denials
        self._request_id = request_id
        self._source_ip = source_ip

    # --- reads --------------------------------------------------------------

    async def list_all(self, *, cursor: str | None, limit: int | None) -> GovernedAssistantPage:
        """A keyset page of EVERY assistant in the tenant with its governance state.

        The admin library view (E6-6): spans all owners in the tenant (INV-1 only —
        admin-gated one layer up). Each item carries its certification/featured/
        disabled state and the projected ``owner_orphaned`` flag (owner no longer a
        tenant member) so the console can flag abandoned assistants (E6-8).
        """
        page_size = _clamp_limit(limit)
        after_id = _decode_cursor(cursor) if cursor else None
        rows = await self._assistants.list_for_tenant_page(limit=page_size + 1, after_id=after_id)
        has_more = len(rows) > page_size
        page = rows[:page_size]
        next_cursor = _encode_cursor(page[-1].id) if has_more and page else None
        items = [await self._with_orphan_flag(a) for a in page]
        return GovernedAssistantPage(items=items, next_cursor=next_cursor)

    # --- governance mutations ----------------------------------------------

    async def certify(self, assistant_id: UUID, *, state: CertificationState) -> Assistant:
        """Set an assistant's certification (certify / deprecate / clear) — admin only.

        ``CERTIFIED`` marks it trusted in the library; ``DEPRECATED`` flags it for
        retirement (still runnable); ``NONE`` clears the verdict. Audited
        ``assistant.certified`` / ``assistant.deprecated`` (INV-6). Cross-tenant/
        missing id → 404.
        """
        assistant = await self._load_or_404(assistant_id, attempted_action="assistant.certify")
        updated = await self._update_or_404(assistant_id, {"certification_state": state})
        action = (
            AuditAction.ASSISTANT_DEPRECATED
            if state is CertificationState.DEPRECATED
            else AuditAction.ASSISTANT_CERTIFIED
        )
        await self._emit(
            action=action,
            assistant_id=assistant_id,
            metadata={"certification_state": state.value, "name": assistant.name},
        )
        return await self._with_orphan_flag(updated)

    async def set_featured(self, assistant_id: UUID, *, featured: bool) -> Assistant:
        """Feature / unfeature an assistant in the library — admin only (E6-6).

        Audited ``assistant.featured`` (INV-6). Cross-tenant/missing id → 404.
        """
        assistant = await self._load_or_404(assistant_id, attempted_action="assistant.feature")
        updated = await self._update_or_404(assistant_id, {"featured": featured})
        await self._emit(
            action=AuditAction.ASSISTANT_FEATURED,
            assistant_id=assistant_id,
            metadata={"featured": featured, "name": assistant.name},
        )
        return await self._with_orphan_flag(updated)

    async def set_disabled(self, assistant_id: UUID, *, disabled: bool) -> Assistant:
        """Disable / re-enable an assistant — admin only (E6-6, INV-8 enforcement).

        Disabling sets ``disabled_at`` and flips ``status`` to ``disabled`` in
        lockstep, so the existing run/chat/schedule gate refuses it (a disabled
        assistant cannot start — the mandatory negative). Re-enabling clears
        ``disabled_at`` and returns the head to ``draft`` (the owner must re-publish
        before it can run again — a disabled assistant is never silently
        re-published). Audited ``assistant.disabled`` (INV-6). Cross-tenant/missing
        id → 404.
        """
        assistant = await self._load_or_404(assistant_id, attempted_action="assistant.disable")
        if disabled:
            fields: dict[str, object] = {
                "status": AssistantStatus.DISABLED,
                "disabled_at": datetime.now(UTC),
            }
        else:
            fields = {"status": AssistantStatus.DRAFT, "disabled_at": None}
        updated = await self._update_or_404(assistant_id, fields)
        await self._emit(
            action=AuditAction.ASSISTANT_DISABLED,
            assistant_id=assistant_id,
            metadata={"disabled": disabled, "name": assistant.name},
        )
        return await self._with_orphan_flag(updated)

    async def transfer_ownership(self, assistant_id: UUID, *, new_owner_id: UUID) -> Assistant:
        """Reassign an assistant's accountable owner to another tenant member (E6-8).

        The new owner must be a **distinct** member of the same tenant (else 422,
        INV-8) — an admin can rescue an orphaned assistant by handing it to a live
        owner. Audited ``assistant.ownership_transferred`` with both the previous and
        the new owner (INV-6). Cross-tenant/missing id → 404.
        """
        assistant = await self._load_or_404(
            assistant_id,
            attempted_action="assistant.transfer_ownership",
        )
        if new_owner_id == assistant.owner_id:
            raise ValidationError(
                "The assistant already has that owner.",
                code="owner_unchanged",
            )
        new_owner = await self._users.get(new_owner_id)
        if new_owner is None:
            # Not a member of this tenant (cross-tenant or unknown) — 422, not 404:
            # the assistant exists, the *input* is invalid (INV-8).
            raise ValidationError(
                "The new owner must be a member of this tenant.",
                code="owner_not_a_member",
            )
        previous_owner_id = assistant.owner_id
        updated = await self._update_or_404(assistant_id, {"owner_id": new_owner_id})
        await self._emit(
            action=AuditAction.ASSISTANT_OWNERSHIP_TRANSFERRED,
            assistant_id=assistant_id,
            metadata={
                "previous_owner_id": str(previous_owner_id),
                "new_owner_id": str(new_owner_id),
                "name": assistant.name,
            },
        )
        return await self._with_orphan_flag(updated)

    async def bulk_disable_orphans(self) -> BulkOrphanResult:
        """Disable every orphaned assistant (owner no longer a tenant member) — E6-8.

        Deny-by-default safety: an abandoned assistant (its owner deprovisioned)
        should not keep running unattended. Walks the tenant's assistants, disables
        those whose owner is no longer a member, and audits each ``assistant.disabled``
        (INV-6). Idempotent: an already-disabled orphan is skipped.
        """
        affected: list[UUID] = []
        for assistant in await self._all_assistants():
            if not await self._is_orphaned(assistant):
                continue
            if assistant.status is AssistantStatus.DISABLED:
                continue
            await self._update_or_404(
                assistant.id,
                {"status": AssistantStatus.DISABLED, "disabled_at": datetime.now(UTC)},
            )
            await self._emit(
                action=AuditAction.ASSISTANT_DISABLED,
                assistant_id=assistant.id,
                metadata={"disabled": True, "reason": "orphaned", "name": assistant.name},
            )
            affected.append(assistant.id)
        return BulkOrphanResult(affected=affected, action="disabled")

    # --- helpers ------------------------------------------------------------

    async def _load_or_404(
        self,
        assistant_id: UUID,
        *,
        attempted_action: str,
    ) -> Assistant:
        """Load a tenant assistant or raise 404 (existence non-disclosure, INV-1)."""
        assistant = await self._assistants.get(assistant_id)
        if assistant is None:
            await self._denials.emit(
                actor_id=self._actor_id,
                resource_type="assistant",
                resource_id=str(assistant_id),
                attempted_action=attempted_action,
                reason="not_visible",
                request_id=self._request_id,
                source_ip=self._source_ip,
            )
            raise NotFoundError("Assistant not found.")
        return assistant

    async def _update_or_404(self, assistant_id: UUID, fields: dict[str, object]) -> Assistant:
        updated = await self._assistants.update(assistant_id, fields=fields)
        if updated is None:  # pragma: no cover — visibility already established
            raise NotFoundError("Assistant not found.")
        return updated

    async def _with_orphan_flag(self, assistant: Assistant) -> Assistant:
        """Project ``owner_orphaned`` (owner no longer a tenant member) onto a view."""
        orphaned = await self._is_orphaned(assistant)
        if orphaned == assistant.owner_orphaned:
            return assistant
        return Assistant(
            id=assistant.id,
            tenant_id=assistant.tenant_id,
            owner_id=assistant.owner_id,
            backup_owner_id=assistant.backup_owner_id,
            name=assistant.name,
            description=assistant.description,
            instructions=assistant.instructions,
            model=assistant.model,
            knowledge_scope=assistant.knowledge_scope,
            tool_allowlist=assistant.tool_allowlist,
            autonomy_level=assistant.autonomy_level,
            status=assistant.status,
            certification_state=assistant.certification_state,
            featured=assistant.featured,
            category=assistant.category,
            disabled_at=assistant.disabled_at,
            created_at=assistant.created_at,
            updated_at=assistant.updated_at,
            current_version=assistant.current_version,
            owner_orphaned=orphaned,
        )

    async def _is_orphaned(self, assistant: Assistant) -> bool:
        """Whether the assistant's owner is no longer a member of the tenant (E6-8)."""
        return await self._users.get(assistant.owner_id) is None

    async def _all_assistants(self) -> list[Assistant]:
        """Every assistant in the tenant (paged internally) — for the bulk orphan sweep."""
        rows: list[Assistant] = []
        after_id: UUID | None = None
        while True:
            page = await self._assistants.list_for_tenant_page(
                limit=_MAX_LIMIT + 1, after_id=after_id
            )
            if not page:
                break
            has_more = len(page) > _MAX_LIMIT
            batch = page[:_MAX_LIMIT]
            rows.extend(batch)
            if not has_more or not batch:
                break
            after_id = batch[-1].id
        return rows

    async def _emit(
        self,
        *,
        action: AuditAction,
        assistant_id: UUID,
        metadata: dict[str, object],
    ) -> None:
        await self._audit.emit(
            action=action,
            actor=AuditActor.user(self._actor_id),
            resource_type="assistant",
            resource_id=str(assistant_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata=metadata,
        )


__all__ = [
    "AssistantGovernanceService",
    "BulkOrphanResult",
    "GovernedAssistantPage",
]
