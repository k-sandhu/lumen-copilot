"""Group management use-cases — create/rename/delete groups, add/remove members.

The orchestration layer behind the group access model (ADR-0022 §2). A **group**
is a tenant-scoped set of users; a ``grants`` row whose ``principal_type`` is
``group`` then makes a resource visible to every member (the retrieval widening
lives in ``retrieval/queries`` — this module only decides *who is in a group*).

**Authorization (deny-by-default, spec 0004 §2.3).** Group management is
**admin-only** (``Role.ADMIN``). The router enforces that structurally with a
router-level ``require_roles(Role.ADMIN)`` dependency, and this service asserts
it again rather than trusting the caller — the seam the tests drive is the
service, not the route (INV-5 → 403).

**Tenancy (INV-1).** ``tenant_id`` and the acting ``actor_id`` come from the
resolved principal (``auth/``), never from request input (spec 0004 §2.3). Every
lookup goes through the tenant-scoped :class:`~app.db.repositories.GroupRepository`,
so a group in another tenant resolves to ``None`` exactly like a non-existent one
and is reported as **404, never 403** (existence non-disclosure, spec 0004 §2.1).

**The system group is immutable.** The per-tenant "All members" group
(``kind='system'``, ADR-0022 §3) expresses tenant-wide visibility and has derived
membership; renaming, deleting, or explicitly populating it is refused as a 409
:class:`~app.core.errors.ConflictError`. It is still listed, so an admin can see
what a tenant-wide grant targets.

Every mutation emits an audit event (INV-6) — a change to who can see what has to
be provable after the fact. The caller owns the transaction boundary; audit
writes are flushed, not committed, so they commit atomically with the change.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.db.repositories import GroupRepository, UserRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, Group, Role
from app.services.audit import AuditSink

# A group name is a human label an admin types; keep it bounded and non-blank so
# the unique index has something meaningful to key on.
MAX_GROUP_NAME_LENGTH = 120


class GroupsService:
    """Create / rename / delete groups and manage their membership.

    Constructed per request with the session, the token-bound ``tenant_id`` +
    ``actor_id`` + the acting principal's ``roles``, and the audit sink with its
    correlation context.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        roles: tuple[Role, ...],
        audit: AuditSink,
        request_id: str,
        source_ip: str,
    ) -> None:
        self._session = session
        self._groups = GroupRepository(session, tenant_id)
        self._users = UserRepository(session, tenant_id)
        self._tenant_id = tenant_id
        self._actor_id = actor_id
        self._is_admin = Role.ADMIN in roles
        self._audit = audit
        self._request_id = request_id
        self._source_ip = source_ip

    # --- internal guards ----------------------------------------------------

    def _require_admin(self) -> None:
        """Group management is admin-only (INV-5 → 403).

        Asserted here as well as at the router so the service — the seam the
        negative tests drive — is safe on its own, not only behind a route.
        """
        if not self._is_admin:
            raise ForbiddenError("Group management requires the admin role.")

    @staticmethod
    def _clean_name(name: str) -> str:
        """Normalize + validate a group name, or 422 (INV-8)."""
        cleaned = name.strip()
        if not cleaned:
            raise ValidationError("Group name must not be blank.", code="invalid_group_name")
        if len(cleaned) > MAX_GROUP_NAME_LENGTH:
            raise ValidationError(
                f"Group name must be at most {MAX_GROUP_NAME_LENGTH} characters.",
                code="invalid_group_name",
            )
        return cleaned

    async def _get_or_404(self, group_id: UUID) -> Group:
        """Load a group in this tenant, or 404 (never 403 — INV-1)."""
        group = await self._groups.get(group_id)
        if group is None:
            raise NotFoundError("Group not found.")
        return group

    @staticmethod
    def _reject_if_system(group: Group) -> None:
        """The derived "All members" group is not editable (ADR-0022 §3)."""
        if group.is_system:
            raise ConflictError(
                "The tenant-wide group is managed automatically and cannot be "
                "renamed, deleted, or edited.",
                code="system_group_immutable",
            )

    async def _emit(self, action: AuditAction, group_id: UUID, **metadata: str | None) -> None:
        await self._audit.emit(
            action=action,
            actor=AuditActor.user(self._actor_id),
            resource_type="group",
            resource_id=str(group_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={k: v for k, v in metadata.items() if v is not None},
        )

    # --- use-cases: groups --------------------------------------------------

    async def list_groups(self) -> list[Group]:
        """Every group in the tenant (system group first, then by name)."""
        self._require_admin()
        return await self._groups.list_all()

    async def get_group(self, group_id: UUID) -> Group:
        self._require_admin()
        return await self._get_or_404(group_id)

    async def create_group(self, *, name: str) -> Group:
        """Create a user group. Duplicate name (case-insensitive) → 409."""
        self._require_admin()
        cleaned = self._clean_name(name)
        if await self._groups.get_by_name(cleaned) is not None:
            raise ConflictError(
                f"A group named {cleaned!r} already exists.", code="group_name_taken"
            )
        try:
            group = await self._groups.create(name=cleaned, created_by=self._actor_id)
        except IntegrityError as exc:  # concurrent create lost the race
            raise ConflictError(
                f"A group named {cleaned!r} already exists.", code="group_name_taken"
            ) from exc
        await self._emit(AuditAction.GROUP_CREATED, group.id, name=group.name)
        return group

    async def rename_group(self, group_id: UUID, *, name: str) -> Group:
        self._require_admin()
        group = await self._get_or_404(group_id)
        self._reject_if_system(group)
        cleaned = self._clean_name(name)
        existing = await self._groups.get_by_name(cleaned)
        if existing is not None and existing.id != group_id:
            raise ConflictError(
                f"A group named {cleaned!r} already exists.", code="group_name_taken"
            )
        renamed = await self._groups.rename(group_id, name=cleaned)
        if renamed is None:  # pragma: no cover — _get_or_404 already proved it exists
            raise NotFoundError("Group not found.")
        await self._emit(
            AuditAction.GROUP_UPDATED, group_id, name=renamed.name, previous_name=group.name
        )
        return renamed

    async def delete_group(self, group_id: UUID) -> None:
        """Delete a group, its membership, and the grants naming it.

        The grant cleanup is what makes the deletion complete: ``grants`` has no
        FK on ``principal_id`` (spec 0004 §2.2), so nothing in the schema would
        remove a grant to a group that no longer exists. The repository does both
        in one place.
        """
        self._require_admin()
        group = await self._get_or_404(group_id)
        self._reject_if_system(group)
        await self._groups.delete(group_id)
        await self._emit(AuditAction.GROUP_DELETED, group_id, name=group.name)

    # --- use-cases: membership ----------------------------------------------

    async def list_member_ids(self, group_id: UUID) -> list[UUID]:
        """The users explicitly in a group.

        Empty for the system group by construction — its membership is derived
        (ADR-0022 §3), so there is nothing to enumerate; the caller renders that
        as "everyone in the tenant" rather than as an empty group.
        """
        self._require_admin()
        await self._get_or_404(group_id)
        return await self._groups.list_member_ids(group_id)

    async def add_member(self, group_id: UUID, *, user_id: UUID) -> None:
        """Add a user to a group. Idempotent; a foreign-tenant user is 404."""
        self._require_admin()
        group = await self._get_or_404(group_id)
        self._reject_if_system(group)
        # Tenant-scoped, so a tenant-B user is indistinguishable from a missing
        # one — 404, never 403, exactly as the grant path treats a grantee.
        if await self._users.get(user_id) is None:
            raise NotFoundError("User not found.")
        added = await self._groups.add_member(
            group_id=group_id, user_id=user_id, added_by=self._actor_id
        )
        if added:
            await self._emit(AuditAction.GROUP_MEMBER_ADDED, group_id, user_id=str(user_id))

    async def remove_member(self, group_id: UUID, *, user_id: UUID) -> None:
        """Remove a user from a group. Idempotent.

        Takes effect on the requester's **next request**: membership is re-read
        per request and never cached on the principal or in the token
        (ADR-0022 §7), so access is never carried by a still-valid token.
        """
        self._require_admin()
        group = await self._get_or_404(group_id)
        self._reject_if_system(group)
        removed = await self._groups.remove_member(group_id=group_id, user_id=user_id)
        if removed:
            await self._emit(AuditAction.GROUP_MEMBER_REMOVED, group_id, user_id=str(user_id))


__all__ = ["MAX_GROUP_NAME_LENGTH", "GroupsService"]
