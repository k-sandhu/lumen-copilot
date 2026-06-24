"""Sharing use-cases — create/revoke explicit ACL grants (issue #18, CC-1).

The orchestration layer behind the permission/ACL model's *grant* half (spec 0004
§2.2). A grant records that some principal (MVP: a ``user``) may access a resource
(a ``collection`` or ``document``) they do not own; the retrieval permission
filter (``app.retrieval.queries`` — the INV-2 chokepoint) then admits that
resource into the grantee's allow-set, so a granted document/collection becomes
retrievable **and** citable by the grantee. This service is the **testable seam**:
there is deliberately no HTTP route and no contract change in this issue — the
grant API + sharing UI are a follow-up (the service is what the negative tests
drive).

**Authorization (deny-by-default, spec 0004 §2.2 + §2.3).** Only the resource's
**owner** — or a **tenant admin** (``Role.ADMIN``) — may grant or revoke access to
it. Everything is tenant-scoped (INV-1): the resource, the grantee, and the grant
row all live in the granting principal's tenant. A resource in another tenant, or
one the granting principal neither owns nor admins, is treated as **non-existent**
— the operation raises :class:`~app.core.errors.NotFoundError` (404), never 403
(existence non-disclosure, spec 0004 §2.1; the system must not reveal that a
foreign/unauthorized resource exists). A denied attempt emits a
``permission.denied`` audit event; a successful grant/revoke emits
``permission.granted`` / ``permission.revoked`` (INV-6).

The ``tenant_id`` and the granting ``owner_id`` come from the resolved principal
(``auth/``), never from request input (spec 0004 §2.3). The caller owns the
transaction boundary; the audit write is flushed, not committed, so it commits
atomically with the grant.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.repositories import (
    CollectionRepository,
    DocumentRepository,
    GrantRepository,
)
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import (
    AuditOutcome,
    Grant,
    GrantPrincipalType,
    GrantResourceType,
    GrantRole,
    Role,
)
from app.services.audit import AuditSink


class GrantsService:
    """Create / revoke explicit ACL grants for one granting principal.

    Constructed per-request with the session, the resolved ``tenant_id`` +
    ``owner_id`` + the granting principal's ``roles`` (all from the token, never
    request input), and the audit sink + correlation context. All
    ownership/tenancy enforcement lives here; the repositories enforce tenancy
    (INV-1) and this service enforces the owner-or-admin grant rule (spec 0004
    §2.2). A foreign/unauthorized resource is reported as 404 (existence
    non-disclosure), never 403.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        roles: tuple[Role, ...],
        audit: AuditSink,
        request_id: str,
        source_ip: str,
    ) -> None:
        self._session = session
        self._grants = GrantRepository(session, tenant_id)
        self._collections = CollectionRepository(session, tenant_id)
        self._documents = DocumentRepository(session, tenant_id)
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._is_admin = Role.ADMIN in roles
        self._audit = audit
        self._request_id = request_id
        self._source_ip = source_ip

    # --- internal: authorization over the granted resource ------------------

    async def _resource_owner(
        self, resource_type: GrantResourceType, resource_id: UUID
    ) -> UUID | None:
        """Return the resource's ``owner_id`` if it exists in this tenant, else ``None``.

        Tenant-scoped (the repositories): a foreign-tenant resource resolves to
        ``None`` so it is indistinguishable from "does not exist" (404). Used to
        decide whether the granting principal owns the resource.
        """
        if resource_type is GrantResourceType.COLLECTION:
            collection = await self._collections.get(resource_id)
            return collection.owner_id if collection is not None else None
        document = await self._documents.get(resource_id)
        return document.owner_id if document is not None else None

    async def _authorize(self, resource_type: GrantResourceType, resource_id: UUID) -> None:
        """Ensure the granting principal may grant/revoke on this resource, or 404.

        The deny-by-default gate (spec 0004 §2.2): the resource must exist in this
        tenant **and** the granting principal must own it — unless they are a
        tenant admin, who may grant on any resource in their tenant. A resource
        that is missing, in another tenant, or owned by someone else (and the
        principal is not an admin) raises :class:`NotFoundError` (404, existence
        non-disclosure) after emitting a ``permission.denied`` audit event.
        """
        owner_id = await self._resource_owner(resource_type, resource_id)
        permitted = owner_id is not None and (self._is_admin or owner_id == self._owner_id)
        if not permitted:
            await self._audit.emit(
                action=AuditAction.PERMISSION_DENIED,
                actor=AuditActor.user(self._owner_id),
                resource_type=resource_type.value,
                resource_id=str(resource_id),
                outcome=AuditOutcome.DENIED,
                request_id=self._request_id,
                source_ip=self._source_ip,
                metadata={"reason": "not_owner_or_admin"},
            )
            # 404, never 403 — do not reveal a foreign/unauthorized resource exists.
            raise NotFoundError("Resource not found.")

    # --- use-cases ----------------------------------------------------------

    async def create_grant(
        self,
        *,
        resource_type: GrantResourceType,
        resource_id: UUID,
        principal_id: UUID,
        principal_type: GrantPrincipalType = GrantPrincipalType.USER,
        role: GrantRole = GrantRole.VIEWER,
    ) -> Grant:
        """Grant ``principal_id`` access to a resource the caller owns (or admins).

        Order (fail-closed):

        1. **Authorize** — the resource must exist in this tenant and be owned by
           the granting principal (or they must be a tenant admin); otherwise 404
           + a ``permission.denied`` audit event (``_authorize``).
        2. **Persist** — insert the grant (idempotent on the unique (resource,
           principal) key — re-granting returns the existing row, never a
           duplicate). The grantee ``principal_id`` and the resource live in the
           granting principal's tenant (INV-1).
        3. **Audit** — emit ``permission.granted`` (INV-6).

        After this the retrieval permission filter admits the resource (and, for a
        collection, its documents — the grant cascade) into the grantee's
        allow-set: the grantee can retrieve and cite it (INV-2).

        Raises:
            NotFoundError: the resource is missing, in another tenant, or not
                owned by the (non-admin) caller — reported as 404.
        """
        await self._authorize(resource_type, resource_id)
        grant = await self._grants.create(
            resource_type=resource_type,
            resource_id=resource_id,
            principal_type=principal_type,
            principal_id=principal_id,
            role=role,
            granted_by=self._owner_id,
        )
        await self._audit.emit(
            action=AuditAction.PERMISSION_GRANTED,
            actor=AuditActor.user(self._owner_id),
            resource_type=resource_type.value,
            resource_id=str(resource_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "principal_type": principal_type.value,
                "principal_id": str(principal_id),
                "role": role.value,
            },
        )
        return grant

    async def revoke_grant(
        self,
        *,
        resource_type: GrantResourceType,
        resource_id: UUID,
        principal_id: UUID,
        principal_type: GrantPrincipalType = GrantPrincipalType.USER,
    ) -> bool:
        """Revoke a principal's grant on a resource the caller owns (or admins).

        Same authorization gate as :meth:`create_grant` (owner-or-admin, else
        404 + ``permission.denied``). Deletes the grant for the (resource,
        principal) pair within this tenant; idempotent — returns ``False`` if no
        such grant existed, ``True`` if one was removed. Audits
        ``permission.revoked`` on a real removal (INV-6). After a revoke the
        retrieval filter no longer admits the resource for that principal — a
        revoked grant excludes the resource again (deny-by-default restored).

        Raises:
            NotFoundError: the resource is missing, in another tenant, or not
                owned by the (non-admin) caller — reported as 404.
        """
        await self._authorize(resource_type, resource_id)
        revoked = await self._grants.revoke(
            resource_type=resource_type,
            resource_id=resource_id,
            principal_type=principal_type,
            principal_id=principal_id,
        )
        if revoked:
            await self._audit.emit(
                action=AuditAction.PERMISSION_REVOKED,
                actor=AuditActor.user(self._owner_id),
                resource_type=resource_type.value,
                resource_id=str(resource_id),
                outcome=AuditOutcome.ALLOWED,
                request_id=self._request_id,
                source_ip=self._source_ip,
                metadata={
                    "principal_type": principal_type.value,
                    "principal_id": str(principal_id),
                },
            )
        return revoked

    async def list_grants(
        self, *, resource_type: GrantResourceType, resource_id: UUID
    ) -> list[Grant]:
        """List the grants on a resource the caller owns (or admins).

        Authorization-gated like the writes (owner-or-admin, else 404). Returns
        every grant on the resource within this tenant (oldest first) — the read
        the follow-up sharing UI renders. No audit event (a read of one's own
        resource's shares).
        """
        await self._authorize(resource_type, resource_id)
        return await self._grants.list_for_resource(
            resource_type=resource_type, resource_id=resource_id
        )


__all__ = ["GrantsService"]
