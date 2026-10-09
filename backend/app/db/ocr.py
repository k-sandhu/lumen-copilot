"""Durable, tenant-bound OCR cache and atomic budget reservations."""

from __future__ import annotations

import json
from dataclasses import asdict
from decimal import ROUND_CEILING, Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.domain.ocr import OcrError, OcrPolicy, OcrResult


def micros(value: Decimal) -> int:
    return int((value * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


class OcrRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant = tenant_id

    async def _policy(self, *, lock: bool = False) -> models.OcrTenantPolicy | None:
        query = select(models.OcrTenantPolicy).where(
            models.OcrTenantPolicy.tenant_id == self._tenant
        )
        if lock:
            query = query.with_for_update()
        return (await self._session.execute(query)).scalar_one_or_none()

    @staticmethod
    def _domain(row: models.OcrTenantPolicy) -> OcrPolicy:
        return OcrPolicy(
            row.tenant_id,
            row.enabled,
            row.page_limit,
            Decimal(row.budget_microusd) / 1_000_000,
            Decimal(row.ceiling_microusd) / 1_000_000,
            row.concurrency,
            row.approved_by,
        )

    async def policy(self) -> OcrPolicy:
        row = await self._policy()
        return self._domain(row) if row else OcrPolicy(self._tenant)

    async def configure(self, policy: OcrPolicy) -> None:
        """Trusted provisioning seam; the service audits in the same transaction."""
        if policy.tenant_id != self._tenant:
            raise OcrError("ocr_tenant_mismatch")
        policy.validate()
        if policy.enabled:
            admin = (
                await self._session.execute(
                    select(models.User).where(
                        models.User.tenant_id == self._tenant, models.User.id == policy.approved_by
                    )
                )
            ).scalar_one_or_none()
            if admin is None or "admin" not in admin.roles:
                raise OcrError("ocr_approval_required")
        row = await self._policy(lock=True)
        if row is None:
            row = models.OcrTenantPolicy(tenant_id=self._tenant)
            self._session.add(row)
        row.enabled = policy.enabled
        row.approved_by = policy.approved_by
        row.page_limit = policy.page_limit
        row.budget_microusd = micros(policy.budget_usd)
        row.ceiling_microusd = micros(policy.per_page_ceiling_usd)
        row.concurrency = policy.concurrency
        await self._session.flush()

    async def _page(self, digest: str, engine: str) -> models.OcrPageCache | None:
        return (
            await self._session.execute(
                select(models.OcrPageCache).where(
                    models.OcrPageCache.tenant_id == self._tenant,
                    models.OcrPageCache.content_sha256 == digest,
                    models.OcrPageCache.engine_id == engine,
                )
            )
        ).scalar_one_or_none()

    async def cached(self, digest: str, engine: str) -> OcrResult | None:
        policy = await self.policy()
        if not policy.enabled:
            raise OcrError("ocr_disabled")
        row = await self._page(digest, engine)
        if row is None:
            return None
        if row.state != "complete" or row.result_json is None:
            raise OcrError("ocr_pending")
        raw = json.loads(row.result_json)
        if raw["cost_usd"] is not None:
            raw["cost_usd"] = Decimal(raw["cost_usd"])
        return OcrResult(**raw)

    async def begin(self, digest: str, engine: str, policy: OcrPolicy) -> None:
        if policy.tenant_id != self._tenant:
            raise OcrError("ocr_tenant_mismatch")
        row = await self._policy(lock=True)
        if row is None or not row.enabled:
            raise OcrError("ocr_disabled")
        # Re-read current trusted approval under lock; stale caller policy cannot widen it.
        current = self._domain(row)
        current.validate()
        if current != policy:
            raise OcrError("ocr_policy_changed")
        if await self._page(digest, engine) is not None:
            raise OcrError("ocr_pending")
        if (
            row.pages_used >= row.page_limit
            or row.cost_used_microusd + row.ceiling_microusd > row.budget_microusd
        ):
            raise OcrError("ocr_budget")
        if row.active_calls >= row.concurrency:
            raise OcrError("ocr_concurrency")
        row.pages_used += 1
        row.cost_used_microusd += row.ceiling_microusd
        row.active_calls += 1
        self._session.add(
            models.OcrPageCache(
                tenant_id=self._tenant,
                content_sha256=digest,
                engine_id=engine,
                reserved_microusd=row.ceiling_microusd,
                state="pending",
            )
        )
        await self._session.flush()

    async def finish(
        self, digest: str, engine: str, result: OcrResult | None, error: str | None
    ) -> None:
        policy = await self._policy(lock=True)
        row = await self._page(digest, engine)
        if policy is None or row is None:
            raise OcrError("ocr_cache_missing")
        if row.state != "pending":
            return
        if result is not None:
            if result.engine_id != engine or not result.text.strip():
                raise OcrError("ocr_invalid_result")
            raw = asdict(result)
            raw["cost_usd"] = str(result.cost_usd) if result.cost_usd is not None else None
            row.result_json = json.dumps(raw, ensure_ascii=False, allow_nan=False)
            row.state = "complete"
            if result.cost_usd is not None:
                if not result.cost_usd.is_finite() or result.cost_usd < 0:
                    raise OcrError("ocr_invalid_result")
                policy.cost_used_microusd += micros(result.cost_usd) - row.reserved_microusd
        else:
            row.state = "unknown"
        row.error = error
        policy.active_calls -= 1
        await self._session.flush()
