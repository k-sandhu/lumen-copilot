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
from app.db.repositories import SYSTEM_GROUP_NAME, GroupRepository, UserRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, Group, Role, User
from app.services.audit import AuditSink, PermissionDeniedContext

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
        denials: PermissionDeniedContext,
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
        self._denials = denials
        self._denials.assert_user(actor_id)
        self._request_id = request_id
        self._source_ip = source_ip

    # --- internal guards ----------------------------------------------------

    async def _require_admin(
        self,
        *,
        attempted_action: str,
        resource_id: str = "collection",
    ) -> None:
        """Group management is admin-only (INV-5 → 403).

        Asserted here as well as at the router so the service — the seam the
        negative tests drive — is safe on its own, not only behind a route.
        """
        if not self._is_admin:
            await self._denials.emit(
                resource_type="group",
                resource_id=resource_id,
                attempted_action=attempted_action,
                reason="missing_required_role",
                required_roles=(Role.ADMIN.value,),
            )
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

    @staticmethod
    def _reject_reserved_name(name: str) -> None:
        """Keep the system group's name reservable (ADR-0022 §3).

        Names are unique per tenant case-insensitively, so a user group called
        "All members" would occupy the name the derived group needs — and
        because that group is created lazily, the squat would block it
        **permanently** for that tenant. Refused up front instead.
        """
        if name.casefold() == SYSTEM_GROUP_NAME.casefold():
            raise ConflictError(
                f"{SYSTEM_GROUP_NAME!r} is reserved for the tenant-wide group.",
                code="group_name_reserved",
            )

    async def _get_or_404(self, group_id: UUID, *, attempted_action: str) -> Group:
        """Load a group in this tenant, or 404 (never 403 — INV-1)."""
        group = await self._groups.get(group_id)
        if group is None:
            await self._denials.emit(
                resource_type="group",
                resource_id=str(group_id),
                attempted_action=attempted_action,
                reason="not_visible",
            )
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
        await self._require_admin(attempted_action="group.list")
        return await self._groups.list_all()

    async def list_groups_with_counts(self) -> list[tuple[Group, int | None]]:
        """Every group paired with its explicit member count — two queries total.

        ``None`` marks the derived system group (see :meth:`member_count`); a
        user group with no members is ``0``. Pairing the count here keeps the
        listing route free of per-group round trips.
        """
        await self._require_admin(attempted_action="group.list")
        # Materialize the tenant's "All members" group on first admin view.
        # ADR-0022 §3 chose lazy creation over provisioning-time creation so
        # existing tenants need no back-fill; without a caller the group would
        # never exist and tenant-wide visibility would have nothing to target.
        system, created_here = await self._groups.ensure_system_group()
        if created_here:
            # Lazily materializing the tenant-wide group IS a mutation, and this
            # route commits it — so it is audited like every other one (INV-6).
            # Only the transaction that inserted emits, so a concurrent first
            # listing cannot double-record the creation.
            await self._emit(AuditAction.GROUP_CREATED, system.id, name=system.name, kind="system")
        groups = await self._groups.list_all()
        counts = await self._groups.member_counts()
        return [(g, None if g.is_system else counts.get(g.id, 0)) for g in groups]

    async def get_group(self, group_id: UUID) -> Group:
        await self._require_admin(attempted_action="group.read", resource_id=str(group_id))
        return await self._get_or_404(group_id, attempted_action="group.read")

    async def create_group(self, *, name: str) -> Group:
        """Create a user group. Duplicate name (case-insensitive) → 409."""
        await self._require_admin(attempted_action="group.create", resource_id="new")
        cleaned = self._clean_name(name)
        self._reject_reserved_name(cleaned)
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
        await self._require_admin(attempted_action="group.update", resource_id=str(group_id))
        group = await self._get_or_404(group_id, attempted_action="group.update")
        self._reject_if_system(group)
        cleaned = self._clean_name(name)
        self._reject_reserved_name(cleaned)
        existing = await self._groups.get_by_name(cleaned)
        if existing is not None and existing.id != group_id:
            raise ConflictError(
                f"A group named {cleaned!r} already exists.", code="group_name_taken"
            )
        try:
            renamed = await self._groups.rename(group_id, name=cleaned)
        except IntegrityError as exc:  # concurrent rename lost the unique race
            raise ConflictError(
                f"A group named {cleaned!r} already exists.", code="group_name_taken"
            ) from exc
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
        await self._require_admin(attempted_action="group.delete", resource_id=str(group_id))
        group = await self._get_or_404(group_id, attempted_action="group.delete")
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
        await self._require_admin(attempted_action="group.members.read", resource_id=str(group_id))
        await self._get_or_404(group_id, attempted_action="group.members.read")
        return await self._groups.list_member_ids(group_id)

    async def list_members(self, group_id: UUID) -> list[User]:
        """The group's members as users, ordered by email — the roster the API returns.

        Resolving ids to users belongs here rather than in the router (a router
        holds no business logic, ADR-0004). The repository does it as a single
        joined, email-ordered query — one read regardless of group size, and a
        membership row whose user no longer exists simply does not join.
        """
        await self._require_admin(attempted_action="group.members.read", resource_id=str(group_id))
        await self._get_or_404(group_id, attempted_action="group.members.read")
        return await self._groups.list_members(group_id)

    async def member_count(self, group_id: UUID) -> int | None:
        """How many users are explicitly in a group, or ``None`` for the system group.

        ``None`` is not "zero": the system group's membership is derived, so it
        has no rows to count and every user in the tenant belongs (ADR-0022 §3).
        The distinction is carried into the API so the console can render
        "everyone" rather than an empty group.
        """
        await self._require_admin(attempted_action="group.members.read", resource_id=str(group_id))
        group = await self._get_or_404(group_id, attempted_action="group.members.read")
        if group.is_system:
            return None
        return len(await self._groups.list_member_ids(group_id))

    async def add_member(self, group_id: UUID, *, user_id: UUID) -> None:
        """Add a user to a group. Idempotent; a foreign-tenant user is 404."""
        await self._require_admin(attempted_action="group.member.add", resource_id=str(group_id))
        group = await self._get_or_404(group_id, attempted_action="group.member.add")
        self._reject_if_system(group)
        # Tenant-scoped, so a tenant-B user is indistinguishable from a missing
        # one — 404, never 403, exactly as the grant path treats a grantee.
        if await self._users.get(user_id) is None:
            await self._denials.emit(
                resource_type="user",
                resource_id=str(user_id),
                attempted_action="group.member.add",
                reason="not_visible",
            )
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
        await self._require_admin(attempted_action="group.member.remove", resource_id=str(group_id))
        group = await self._get_or_404(group_id, attempted_action="group.member.remove")
        self._reject_if_system(group)
        removed = await self._groups.remove_member(group_id=group_id, user_id=user_id)
        if removed:
            await self._emit(AuditAction.GROUP_MEMBER_REMOVED, group_id, user_id=str(user_id))


__all__ = ["MAX_GROUP_NAME_LENGTH", "GroupsService"]
