"""Bounded tenant-scoped diagnostics and original-byte inventory, never evidence writes."""

from __future__ import annotations

from dataclasses import asdict
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.db.repositories import to_document
from app.domain.entities import Document
from app.domain.ingestion_shadow import FORMATS, ShadowComparison, ShadowRecord


class ShadowRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session, self._tenant = session, tenant_id

    async def record(self, document_id: UUID, fingerprint: str, value: ShadowComparison) -> None:
        document = await self._session.scalar(
            select(models.Document.id)
            .where(models.Document.tenant_id == self._tenant, models.Document.id == document_id)
            .with_for_update()
        )
        if document is None:
            raise ValueError("shadow document is unavailable")
        if len(fingerprint) != 64 or value.source_format not in FORMATS:
            raise ValueError("invalid shadow identity")
        payload = asdict(value)
        if value.status not in {
            "indexed",
            "partial",
            "needs_ocr",
            "unsupported",
            "encrypted",
            "failed",
        }:
            raise ValueError("invalid shadow status")
        numbers = (
            value.baseline_chars,
            value.candidate_chars,
            value.positional_mismatches,
            value.block_count,
        )
        if any(type(n) is not int or n < 0 for n in numbers) or type(value.exact_equal) is not bool:
            raise ValueError("invalid shadow counters")
        allowed = {
            None,
            "native_unavailable",
            "native_failed",
            "budget",
            "timeout",
            "encrypted",
            "parse",
            "invalid_structure",
            "native_panic",
            "output_limit",
            "worker_memory",
            "worker_failed",
        }
        if value.failure_code not in allowed:
            raise ValueError("invalid shadow failure category")
        insert = sqlite_insert if self._session.get_bind().dialect.name == "sqlite" else pg_insert
        statement = (
            insert(models.IngestionShadow)
            .values(
                id=uuid4(),
                tenant_id=self._tenant,
                document_id=document_id,
                fingerprint=fingerprint,
                source_format=value.source_format,
                comparison_json=payload,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "document_id", "fingerprint"])
        )
        await self._session.execute(statement)

    async def page(
        self, *, limit: int, cursor: UUID | None = None, document_id: UUID | None = None
    ) -> list[ShadowRecord]:
        if not 1 <= limit <= 100:
            raise ValueError("shadow page limit must be between 1 and 100")
        stmt = select(models.IngestionShadow).where(
            models.IngestionShadow.tenant_id == self._tenant
        )
        if cursor is not None:
            stmt = stmt.where(models.IngestionShadow.id > cursor)
        if document_id is not None:
            stmt = stmt.where(models.IngestionShadow.document_id == document_id)
        rows = (
            await self._session.scalars(stmt.order_by(models.IngestionShadow.id).limit(limit))
        ).all()
        return [
            ShadowRecord(r.id, r.document_id, ShadowComparison(**r.comparison_json)) for r in rows
        ]  # type: ignore[arg-type]

    async def inventory(
        self,
        *,
        limit: int,
        cursor: UUID | None = None,
        document_id: UUID | None = None,
        collection_id: UUID | None = None,
    ) -> list[Document]:
        if not 1 <= limit <= 100:
            raise ValueError("inventory limit must be between 1 and 100")
        stmt = select(models.Document).where(models.Document.tenant_id == self._tenant)
        if cursor is not None:
            stmt = stmt.where(models.Document.id > cursor)
        if document_id is not None:
            stmt = stmt.where(models.Document.id == document_id)
        if collection_id is not None:
            stmt = stmt.where(models.Document.collection_id == collection_id)
        return [
            to_document(r)
            for r in (
                await self._session.scalars(stmt.order_by(models.Document.id).limit(limit))
            ).all()
        ]
