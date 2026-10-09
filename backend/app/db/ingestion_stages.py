"""Tenant-scoped, attempt-fenced operational ingestion cache."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.domain.entities import DocumentStatus
from app.domain.ingestion_stages import STAGES, StageOutput, StageOwnershipLost


class StageRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def ensure_owned(self, document_id: UUID, *, attempt: int) -> None:
        row = (
            await self._session.execute(
                select(models.Document)
                .where(
                    models.Document.tenant_id == self._tenant_id,
                    models.Document.id == document_id,
                    models.Document.status == DocumentStatus.PROCESSING.value,
                    models.Document.ingestion_attempts == attempt,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise StageOwnershipLost("ingestion attempt no longer owns the document")

    async def get(self, document_id: UUID, stage: str, fingerprint: str) -> StageOutput | None:
        row = (
            await self._session.execute(
                select(models.IngestionStageOutput).where(
                    models.IngestionStageOutput.tenant_id == self._tenant_id,
                    models.IngestionStageOutput.document_id == document_id,
                    models.IngestionStageOutput.stage == stage,
                    models.IngestionStageOutput.fingerprint == fingerprint,
                )
            )
        ).scalar_one_or_none()
        return (
            StageOutput(row.stage, row.fingerprint, row.output_sha256, row.payload_json)
            if row
            else None
        )

    async def save(self, document_id: UUID, output: StageOutput, *, attempt: int) -> None:
        await self.ensure_owned(document_id, attempt=attempt)
        if output.stage not in STAGES:
            raise ValueError("unknown stage")
        # Replacement and invalidation are one transaction; no detached artifacts.
        await self._session.execute(
            delete(models.IngestionStageOutput).where(
                models.IngestionStageOutput.tenant_id == self._tenant_id,
                models.IngestionStageOutput.document_id == document_id,
                models.IngestionStageOutput.stage.in_(STAGES[STAGES.index(output.stage) :]),
            )
        )
        self._session.add(
            models.IngestionStageOutput(
                tenant_id=self._tenant_id,
                document_id=document_id,
                stage=output.stage,
                fingerprint=output.fingerprint,
                output_sha256=output.output_sha256,
                payload_json=output.payload_json,
            )
        )
        document = (
            await self._session.execute(
                select(models.Document).where(
                    models.Document.tenant_id == self._tenant_id, models.Document.id == document_id
                )
            )
        ).scalar_one()
        document.ingestion_stage = output.stage
        await self._session.flush()

    async def clear(self, document_id: UUID) -> None:
        document = (
            await self._session.execute(
                select(models.Document)
                .where(
                    models.Document.tenant_id == self._tenant_id, models.Document.id == document_id
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if document is None:
            return
        await self._session.execute(
            delete(models.IngestionStageOutput).where(
                models.IngestionStageOutput.tenant_id == self._tenant_id,
                models.IngestionStageOutput.document_id == document_id,
            )
        )
        document.ingestion_stage = None
        await self._session.flush()

    async def list(self, document_id: UUID) -> list[StageOutput]:
        rows = (
            (
                await self._session.execute(
                    select(models.IngestionStageOutput).where(
                        models.IngestionStageOutput.tenant_id == self._tenant_id,
                        models.IngestionStageOutput.document_id == document_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return sorted(
            (StageOutput(r.stage, r.fingerprint, r.output_sha256, r.payload_json) for r in rows),
            key=lambda r: STAGES.index(r.stage),
        )
