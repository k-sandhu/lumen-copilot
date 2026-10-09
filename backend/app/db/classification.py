"""Tenant predicates, document locks, leases and shared classification budget."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.domain.classification import ClassificationMetadata, ClassificationWork
from app.domain.decisions import DecisionAttempt, DecisionUsage


def _work(row: models.DocumentClassification) -> ClassificationWork:
    return ClassificationWork(
        row.tenant_id,
        row.document_id,
        row.input_fingerprint,
        row.extraction_id,
        row.taxonomy_version,
        row.input_json,
        row.result.copy(),
        row.status,
        row.revision,
        row.retries,
        row.run_id,
        row.override_path,
    )


class ClassificationRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def metadata_for_documents(
        self, document_ids: list[UUID]
    ) -> dict[UUID, ClassificationMetadata]:
        """One bounded tenant query; callers supply already-permitted document IDs."""
        if not document_ids:
            return {}
        if len(document_ids) > 500:
            raise ValueError("classification metadata batch too large")
        rows = (
            await self.session.execute(
                select(
                    models.DocumentClassification.document_id,
                    models.DocumentClassification.status,
                    models.DocumentClassification.result,
                ).where(
                    models.DocumentClassification.tenant_id == self.tenant_id,
                    models.DocumentClassification.document_id.in_(document_ids),
                )
            )
        ).all()
        result = {}
        for document_id, status, value in rows:
            metadata = ClassificationMetadata.from_result(status, value)
            if metadata is not None:
                result[document_id] = metadata
        return result

    async def _document_lock(self, document_id: UUID) -> bool:
        return (
            await self.session.execute(
                select(models.Document.id)
                .where(
                    models.Document.tenant_id == self.tenant_id,
                    models.Document.id == document_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none() is not None

    async def _row(self, document_id: UUID) -> models.DocumentClassification | None:
        return (
            await self.session.execute(
                select(models.DocumentClassification)
                .where(
                    models.DocumentClassification.tenant_id == self.tenant_id,
                    models.DocumentClassification.document_id == document_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def policy(self) -> dict[str, Any]:
        row = (
            await self.session.execute(
                select(models.ClassificationPolicy).where(
                    models.ClassificationPolicy.tenant_id == self.tenant_id,
                )
            )
        ).scalar_one_or_none()
        return dict(row.controls) if row else {"enabled": False}

    async def set_policy(self, controls: dict[str, Any]) -> None:
        await self.session.execute(
            select(models.Tenant.id).where(models.Tenant.id == self.tenant_id).with_for_update()
        )
        row = (
            await self.session.execute(
                select(models.ClassificationPolicy).where(
                    models.ClassificationPolicy.tenant_id == self.tenant_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = models.ClassificationPolicy(tenant_id=self.tenant_id, controls=controls)
            self.session.add(row)
        else:
            row.controls = controls
        await self.session.flush()

    async def get(self, document_id: UUID) -> ClassificationWork | None:
        row = await self._row(document_id)
        return _work(row) if row else None

    async def schedule(
        self,
        document_id: UUID,
        *,
        extraction_id: str,
        input_json: str,
        taxonomy_version: str,
        controls: dict[str, Any],
    ) -> bool:
        if len(input_json.encode()) > 32 * 1024 * 1024 or len(extraction_id) != 64:
            raise ValueError("invalid classification input budget or lineage")
        if not await self._document_lock(document_id):
            return False
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "extraction": extraction_id,
                    "taxonomy": taxonomy_version,
                    "controls": controls,
                    "prompt": "classification-1",
                    "rules": "1",
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        row = await self._row(document_id)
        if row is not None and (row.override_path or row.input_fingerprint == fingerprint):
            return False
        if row is None:
            row = models.DocumentClassification(
                tenant_id=self.tenant_id, document_id=document_id, revision=0, retries=0
            )
            self.session.add(row)
        else:
            row.revision += 1
        row.input_fingerprint = fingerprint
        row.extraction_id = extraction_id
        row.taxonomy_version = taxonomy_version
        row.input_json = input_json
        row.status = "pending" if controls.get("enabled") else "unclassified"
        # Prior completed result keeps its original taxonomy version while new work is pending.
        if not row.result:
            row.result = {
                "status": "unclassified",
                "reason": "pending" if controls.get("enabled") else "decision_disabled",
                "path": None,
                "taxonomy_version": taxonomy_version,
            }
        row.retries = 0
        row.run_id = None
        row.lease_until = None
        row.next_retry_at = None
        await self.session.flush()
        return True

    async def claim(
        self, document_id: UUID, *, run_id: UUID, lease_seconds: int, max_active: int = 1
    ) -> ClassificationWork | None:
        await self.session.execute(
            select(models.Tenant.id).where(models.Tenant.id == self.tenant_id).with_for_update()
        )
        active = (
            await self.session.execute(
                select(func.count())
                .select_from(models.DocumentClassification)
                .where(
                    models.DocumentClassification.tenant_id == self.tenant_id,
                    models.DocumentClassification.status == "processing",
                    models.DocumentClassification.lease_until > datetime.now(UTC),
                )
            )
        ).scalar_one()
        if active >= max_active:
            return None
        if not await self._document_lock(document_id):
            return None
        row = await self._row(document_id)
        now = datetime.now(UTC)
        if (
            row is None
            or row.override_path
            or row.status not in {"pending", "processing", "incomplete", "unclassified"}
        ):
            return None

        def future(value: datetime | None) -> bool:
            return value is not None and value.replace(tzinfo=UTC) > now

        if future(row.lease_until) or future(row.next_retry_at):
            return None
        if row.status in {"incomplete", "unclassified"} and row.next_retry_at is None:
            return None
        row.run_id = run_id
        row.lease_until = now + timedelta(seconds=lease_seconds)
        row.status = "processing"
        await self.session.flush()
        return _work(row)

    async def complete(
        self,
        work: ClassificationWork,
        result: dict[str, Any],
        *,
        max_retries: int,
        backoff_seconds: float,
    ) -> bool:
        if work.tenant_id != self.tenant_id or not await self._document_lock(work.document_id):
            return False
        row = await self._row(work.document_id)
        if (
            row is None
            or row.override_path
            or row.revision != work.revision
            or row.input_fingerprint != work.input_fingerprint
            or row.run_id != work.run_id
        ):
            return False
        row.result = result
        row.status = result["status"]
        row.run_id = None
        row.lease_until = None
        row.next_retry_at = None
        if result.get("retryable") and row.retries < max_retries:
            row.next_retry_at = datetime.now(UTC) + timedelta(
                seconds=backoff_seconds * 2**row.retries
            )
            row.retries += 1
        await self.session.flush()
        return True

    async def override(
        self,
        document_id: UUID,
        *,
        expected_revision: int,
        path: str,
        actor: UUID,
        reason: str,
        taxonomy_version: str,
    ) -> bool:
        if not await self._document_lock(document_id):
            return False
        row = await self._row(document_id)
        if row is None or row.revision != expected_revision:
            return False
        row.override_path = path
        row.override_actor = actor
        row.override_reason = reason
        row.revision += 1
        row.run_id = None
        row.lease_until = None
        row.next_retry_at = None
        row.status = "override"
        row.result = {
            "status": "override",
            "path": path,
            "taxonomy_version": taxonomy_version,
            "method": "override",
            "levels": [],
            "confidence": None,
            "facets": {},
            "total_cost_usd": "0",
            "review_required": False,
            "override_actor": str(actor),
        }
        await self.session.flush()
        return True

    async def batch(
        self, *, after: UUID | None = None, limit: int = 100, due_only: bool = False
    ) -> list[ClassificationWork]:
        query = select(models.DocumentClassification).where(
            models.DocumentClassification.tenant_id == self.tenant_id,
            models.DocumentClassification.override_path.is_(None),
        )
        if after:
            query = query.where(models.DocumentClassification.document_id > after)
        if due_only:
            now = datetime.now(UTC)
            query = query.where(
                or_(
                    models.DocumentClassification.status == "pending",
                    models.DocumentClassification.next_retry_at <= now,
                    (models.DocumentClassification.status == "processing")
                    & (models.DocumentClassification.lease_until <= now),
                )
            )
        rows = (
            (
                await self.session.execute(
                    query.order_by(models.DocumentClassification.document_id).limit(
                        min(max(limit, 1), 500)
                    )
                )
            )
            .scalars()
            .all()
        )
        return [_work(r) for r in rows]


class DecisionBudgetRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def lock_tenant(self) -> None:
        await self.session.execute(
            select(models.Tenant.id).where(models.Tenant.id == self.tenant_id).with_for_update()
        )

    async def reserve(self, attempt: DecisionAttempt, ceiling: Decimal, budget: Decimal) -> bool:
        if attempt.tenant_id != self.tenant_id:
            raise ValueError("decision tenant mismatch")
        if not ceiling.is_finite() or not budget.is_finite() or ceiling <= 0 or budget <= 0:
            raise ValueError("invalid budget")
        tenant = (
            await self.session.execute(
                select(models.Tenant.id).where(models.Tenant.id == self.tenant_id).with_for_update()
            )
        ).scalar_one_or_none()
        if tenant is None:
            return False
        existing = (
            await self.session.execute(
                select(models.ClassificationSpend).where(
                    models.ClassificationSpend.tenant_id == self.tenant_id,
                    models.ClassificationSpend.id == attempt.id,
                )
            )
        ).scalar_one_or_none()
        metadata = {
            "method": attempt.method,
            "model": attempt.model,
            "input_fingerprint": attempt.input_fingerprint,
            "ordinal": attempt.ordinal,
            "option_orders": {k: list(v) for k, v in attempt.option_orders.items()},
            "order_policy": attempt.order_policy,
            "order_seed": attempt.order_seed,
        }
        if existing:
            return existing.ceiling_usd == ceiling and existing.attempt == metadata
        spent = (
            await self.session.execute(
                select(
                    func.coalesce(
                        func.sum(
                            func.coalesce(
                                models.ClassificationSpend.cost_usd,
                                models.ClassificationSpend.ceiling_usd,
                            )
                        ),
                        0,
                    )
                ).where(models.ClassificationSpend.tenant_id == self.tenant_id)
            )
        ).scalar_one()
        if Decimal(str(spent)) + ceiling > budget:
            return False
        self.session.add(
            models.ClassificationSpend(
                id=attempt.id, tenant_id=self.tenant_id, ceiling_usd=ceiling, attempt=metadata
            )
        )
        await self.session.flush()
        return True

    async def settle(self, attempt: DecisionAttempt, usage: DecisionUsage | None) -> None:
        if attempt.tenant_id != self.tenant_id:
            raise ValueError("decision tenant mismatch")
        await self.session.execute(
            select(models.Tenant.id).where(models.Tenant.id == self.tenant_id).with_for_update()
        )
        row = (
            await self.session.execute(
                select(models.ClassificationSpend)
                .where(
                    models.ClassificationSpend.tenant_id == self.tenant_id,
                    models.ClassificationSpend.id == attempt.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError("decision reservation missing")
        if usage is None:
            return
        if not usage.cost_usd.is_finite() or usage.cost_usd < 0:
            raise ValueError("invalid actual cost")
        value: dict[str, object] = {
            "prompt_tokens": usage.tokens.prompt_tokens,
            "completion_tokens": usage.tokens.completion_tokens,
            "reported_model": usage.reported_model,
        }
        if row.usage is not None and (row.cost_usd != usage.cost_usd or row.usage != value):
            raise ValueError("decision settlement mismatch")
        row.cost_usd = usage.cost_usd
        row.usage = value
        await self.session.flush()


async def classification_tenants(*, after: UUID | None = None, limit: int = 500) -> list[UUID]:
    """System inventory reads only root tenant IDs; never bypasses content RLS."""
    from app.db.session import session_scope

    async with session_scope() as session:
        query = select(models.Tenant.id).order_by(models.Tenant.id).limit(min(limit, 500))
        if after is not None:
            query = query.where(models.Tenant.id > after)
        return list((await session.execute(query)).scalars().all())
