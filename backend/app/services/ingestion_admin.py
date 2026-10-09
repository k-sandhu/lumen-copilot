"""Administrator diagnostics and original-byte replay; generation activation is gated."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.core.config import Settings
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.db.ingestion_shadow import ShadowRepository
from app.db.repositories import AuditEventRepository, GroupRepository
from app.db.session import tenant_session_scope
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, Role
from app.domain.ingestion_shadow import (
    CandidateExtraction,
    ShadowComparison,
    ShadowRecord,
    source_format,
)
from app.ingestion.native import candidate_identity, extract_format_candidate
from app.ingestion.parsers import parse_document
from app.ingestion.stage_identity import parser_identity
from app.retrieval.permissions import AllowSet
from app.retrieval.queries import get_permitted_document, permitted_document_ids
from app.services.audit import AuditSink, PermissionDeniedContext, audited_resource
from app.services.auth_service import require_role
from app.services.ingestion_shadow import extract_with_mode
from app.storage import ObjectStore


@dataclass(frozen=True, slots=True)
class DiagnosticPage:
    rows: tuple[ShadowRecord, ...]
    next_cursor: UUID | None


@dataclass(frozen=True, slots=True)
class InventoryPage:
    documents: tuple[tuple[UUID, str], ...]
    next_cursor: UUID | None


class IngestionAdminService:
    def __init__(
        self,
        principal: Principal,
        *,
        settings: Settings,
        object_store: ObjectStore,
        denials: PermissionDeniedContext,
    ) -> None:
        denials.assert_user(principal.tenant_id, principal.user_id)
        self._principal, self._settings, self._store, self._denials = (
            principal,
            settings,
            object_store,
            denials,
        )

    async def _allow_set(self, session: AsyncSession) -> AllowSet:
        groups = await GroupRepository(session, self._principal.tenant_id).group_ids_for_user(
            self._principal.user_id
        )
        return AllowSet.for_principal(self._principal, group_ids=groups)

    async def _audit(
        self,
        session: AsyncSession,
        *,
        action: AuditAction,
        resource: str,
        resource_id: str,
        metadata: dict[str, object],
    ) -> None:
        await AuditSink(AuditEventRepository(session, self._principal.tenant_id)).emit(
            action=action,
            actor=AuditActor.user(self._principal.user_id),
            resource_type=resource,
            resource_id=resource_id,
            outcome=AuditOutcome.ALLOWED,
            request_id="ingestion-admin-cli",
            source_ip="system",
            metadata=metadata,
        )

    @audited_resource("ingestion.shadow.report", "document", "document_id", missing_result=True)
    async def report(
        self, *, document_id: UUID | None = None, cursor: UUID | None = None, limit: int = 100
    ) -> DiagnosticPage:
        require_role(self._principal, Role.ADMIN)
        async with tenant_session_scope(self._principal.tenant_id) as session:
            allow = await self._allow_set(session)
            if (
                document_id is not None
                and await get_permitted_document(session, allow_set=allow, document_id=document_id)
                is None
            ):
                raise NotFoundError()
            rows = await ShadowRepository(session, self._principal.tenant_id).page(
                document_id=document_id, cursor=cursor, limit=limit
            )
            allowed = await permitted_document_ids(
                session, allow_set=allow, document_ids=[r.document_id for r in rows]
            )
            visible = tuple(row for row in rows if row.document_id in allowed)
            await self._audit(
                session,
                action=AuditAction.DOCUMENT_VIEWED,
                resource="ingestion_shadow",
                resource_id=str(document_id) if document_id else "tenant-report",
                metadata={"operation": "shadow_report", "records": len(visible)},
            )
            return DiagnosticPage(visible, rows[-1].id if len(rows) == limit else None)

    @audited_resource(
        "ingestion.reingestion.preview", "document", "document_id", missing_result=True
    )
    async def preview(
        self,
        *,
        document_id: UUID | None = None,
        collection_id: UUID | None = None,
        cursor: UUID | None = None,
        limit: int = 100,
    ) -> InventoryPage:
        require_role(self._principal, Role.ADMIN)
        async with tenant_session_scope(self._principal.tenant_id) as session:
            allow = await self._allow_set(session)
            if (
                document_id is not None
                and await get_permitted_document(session, allow_set=allow, document_id=document_id)
                is None
            ):
                raise NotFoundError()
            rows = await ShadowRepository(session, self._principal.tenant_id).inventory(
                document_id=document_id, collection_id=collection_id, cursor=cursor, limit=limit
            )
            allowed = await permitted_document_ids(
                session, allow_set=allow, document_ids=[r.id for r in rows]
            )
            visible = tuple(
                (row.id, source_format(row.mime_type)) for row in rows if row.id in allowed
            )
            await self._audit(
                session,
                action=AuditAction.DOCUMENT_VIEWED,
                resource="ingestion_inventory",
                resource_id=str(document_id) if document_id else "tenant-preview",
                metadata={"operation": "reingestion_preview", "documents": len(visible)},
            )
            return InventoryPage(visible, rows[-1].id if len(rows) == limit else None)

    @audited_resource("ingestion.original.replay", "document", "document_id", missing_result=True)
    async def replay(self, document_id: UUID) -> ShadowComparison:
        require_role(self._principal, Role.ADMIN)
        # Authorization and audit commit before original bytes are read.
        async with tenant_session_scope(self._principal.tenant_id) as session:
            document = await get_permitted_document(
                session, allow_set=await self._allow_set(session), document_id=document_id
            )
            if document is None:
                raise NotFoundError()
            if document.size_bytes > self._settings.native_ingestion_max_input_bytes:
                raise ConflictError("Original exceeds replay input budget.", code="replay_budget")
            await self._audit(
                session,
                action=AuditAction.DOCUMENT_DOWNLOADED,
                resource="document",
                resource_id=str(document_id),
                metadata={"operation": "original_shadow_replay"},
            )
        data = await self._store.get(str(self._principal.tenant_id), document.storage_key)
        if len(data) > self._settings.native_ingestion_max_input_bytes:
            raise ConflictError("Original exceeds replay input budget.", code="replay_budget")
        fingerprint = hashlib.sha256(
            (
                hashlib.sha256(data).hexdigest()
                + json.dumps(
                    {
                        "candidate": candidate_identity(self._settings),
                        "baseline": parser_identity(document.mime_type),
                    },
                    sort_keys=True,
                )
            ).encode()
        ).hexdigest()

        async def baseline() -> str:
            return parse_document(data, mime_type=document.mime_type)

        async def candidate() -> CandidateExtraction:
            return await extract_format_candidate(
                data, mime_type=document.mime_type, settings=self._settings
            )

        async def record(value: ShadowComparison) -> None:
            # Revalidate access after CPU work. Nothing mutates document/chunks/citations.
            async with tenant_session_scope(self._principal.tenant_id) as session:
                current = await get_permitted_document(
                    session, allow_set=await self._allow_set(session), document_id=document_id
                )
                if current is None:
                    raise NotFoundError()
                await ShadowRepository(session, self._principal.tenant_id).record(
                    document_id, fingerprint, value
                )
                await self._audit(
                    session,
                    action=AuditAction.DOCUMENT_PROCESSING_STAGE_COMPLETED,
                    resource="document",
                    resource_id=str(document_id),
                    metadata={"operation": "original_shadow_replay", "stage": "shadow_compare"},
                )

        # Operator replay surfaces storage/permission errors after live-shadow isolation.
        recorded: list[ShadowComparison] = []

        async def capture(value: ShadowComparison) -> None:
            recorded.append(value)

        await extract_with_mode(
            mode="shadow",
            source_format=source_format(document.mime_type),
            baseline=baseline,
            candidate=candidate,
            record=capture,
            max_chars=self._settings.native_ingestion_max_output_chars,
        )
        await record(recorded[0])
        return recorded[0]

    @audited_resource(
        "ingestion.reingestion.execute", "document", "document_id", missing_result=True
    )
    async def execute_generation(self, *, document_id: UUID | None = None) -> None:
        require_role(self._principal, Role.ADMIN)
        if document_id is not None:
            async with tenant_session_scope(self._principal.tenant_id) as session:
                if (
                    await get_permitted_document(
                        session, allow_set=await self._allow_set(session), document_id=document_id
                    )
                    is None
                ):
                    raise NotFoundError()
        raise ForbiddenError(
            "Immutable generation retention/backfill policy requires owner approval.",
            code="generation_policy_required",
        )


def summarize(rows: Sequence[ShadowRecord]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        value = row.comparison
        bucket = counts.setdefault(value.source_format, {})
        for key, amount in (
            ("records", 1),
            (value.status, 1),
            ("exact_equal", int(value.exact_equal)),
            ("baseline_chars", value.baseline_chars),
            ("candidate_chars", value.candidate_chars),
            ("positional_mismatches", value.positional_mismatches),
        ):
            bucket[key] = bucket.get(key, 0) + amount
        if value.failure_code:
            key = "failure_" + value.failure_code
            bucket[key] = bucket.get(key, 0) + 1
    return counts
