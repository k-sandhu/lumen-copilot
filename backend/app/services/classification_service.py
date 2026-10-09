"""Permissioned classification reads/overrides and explicit administrator controls."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.classification.taxonomy import _nodes, load_taxonomy
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.db.classification import ClassificationRepository
from app.db.repositories import GroupRepository
from app.domain.audit import AuditAction
from app.domain.entities import AuditOutcome, Role
from app.retrieval.permissions import AllowSet
from app.retrieval.queries import get_permitted_document
from app.services.audit import AuditSink, PermissionDeniedContext, audited_resource
from app.services.classification_controls import ClassificationControls


class ClassificationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        audit: AuditSink,
        denials: PermissionDeniedContext,
    ) -> None:
        denials.assert_user(principal.tenant_id, principal.user_id)
        if audit.tenant_id != principal.tenant_id:
            raise ValueError("classification audit tenant mismatch")
        self._session = session
        self._principal = principal
        self._audit = audit
        self._denials = denials
        self._repo = ClassificationRepository(session, principal.tenant_id)

    async def _visible(self, document_id: UUID) -> Any:
        groups = await GroupRepository(self._session, self._principal.tenant_id).group_ids_for_user(
            self._principal.user_id
        )
        document = await get_permitted_document(
            self._session,
            allow_set=AllowSet.for_principal(self._principal, group_ids=frozenset(groups)),
            document_id=document_id,
        )
        if document is None:
            raise NotFoundError("Document not found.")
        return document

    async def _emit(self, document_id: UUID, operation: str, metadata: dict[str, object]) -> None:
        await self._audit.emit(
            action=AuditAction.DOCUMENT_CLASSIFICATION_UPDATED,
            actor=self._denials.actor,
            resource_type="document",
            resource_id=str(document_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._denials.request_id,
            source_ip=self._denials.source_ip,
            metadata={"operation": operation, **metadata},
        )

    @audited_resource(
        attempted_action="document.classification_read",
        resource_type="document",
        target_parameter="document_id",
        missing_result=False,
    )
    async def get(self, document_id: UUID) -> dict[str, Any] | None:
        await self._visible(document_id)
        await self._audit.emit(
            action=AuditAction.DOCUMENT_VIEWED,
            actor=self._denials.actor,
            resource_type="document",
            resource_id=str(document_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._denials.request_id,
            source_ip=self._denials.source_ip,
            metadata={"surface": "classification"},
        )
        work = await self._repo.get(document_id)
        return {**work.result, "status": work.status, "revision": work.revision} if work else None

    @audited_resource(
        attempted_action="document.classification_override",
        resource_type="document",
        target_parameter="document_id",
        missing_result=False,
    )
    async def override(
        self,
        document_id: UUID,
        *,
        path: str,
        reason: str,
        expected_revision: int,
        taxonomy_version: str,
    ) -> None:
        document = await self._visible(document_id)
        if document.owner_id != self._principal.user_id and not self._principal.has_role(
            Role.ADMIN
        ):
            raise ForbiddenError("Document edit rights are required.")
        if (
            not reason.strip()
            or len(reason) > 1000
            or expected_revision < 0
            or path not in _nodes(load_taxonomy(taxonomy_version))
        ):
            raise ValidationError("Invalid classification override.")
        if not await self._repo.override(
            document_id,
            expected_revision=expected_revision,
            path=path,
            actor=self._principal.user_id,
            reason=reason,
            taxonomy_version=taxonomy_version,
        ):
            raise ConflictError("Classification revision changed.")
        await self._emit(
            document_id,
            "override",
            {
                "path": path,
                "taxonomy_version": taxonomy_version,
                "expected_revision": expected_revision,
            },
        )

    async def set_controls(self, controls: ClassificationControls) -> None:
        if not self._principal.has_role(Role.ADMIN):
            await self._denials.emit(
                resource_type="classification_policy",
                resource_id=str(self._principal.tenant_id),
                attempted_action="classification.policy_updated",
                reason="wrong_role",
                required_roles=["admin"],
            )
            raise ForbiddenError("Administrator role is required.")
        load_taxonomy(controls.taxonomy_version)
        approved = controls.model_copy(update={"approved_by": self._principal.user_id})
        await self._repo.set_policy(approved.model_dump(mode="json"))
        await self._audit.emit(
            action=AuditAction.CLASSIFICATION_POLICY_UPDATED,
            actor=self._denials.actor,
            resource_type="classification_policy",
            resource_id=str(self._principal.tenant_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._denials.request_id,
            source_ip=self._denials.source_ip,
            metadata={
                "enabled": controls.enabled,
                "model": controls.model,
                "taxonomy_version": controls.taxonomy_version,
            },
        )
