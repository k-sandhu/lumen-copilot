"""Provider-neutral selective OCR contract (ADR-0029). No I/O or vendor shapes."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID


class OcrError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class OcrPage:
    number: int
    pdf: bytes


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    engine_id: str
    confidence: float | None = None
    cost_usd: Decimal | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class OcrProvider(Protocol):
    @property
    def engine_id(self) -> str: ...
    async def recognize(self, page: OcrPage) -> OcrResult: ...


@dataclass(frozen=True, slots=True)
class OcrPolicy:
    tenant_id: UUID
    enabled: bool = False
    page_limit: int = 0
    budget_usd: Decimal = Decimal(0)
    per_page_ceiling_usd: Decimal = Decimal("0.01")
    concurrency: int = 1
    approved_by: UUID | None = None

    def validate(self) -> None:
        if (
            not self.budget_usd.is_finite()
            or self.budget_usd < 0
            or not self.per_page_ceiling_usd.is_finite()
            or self.per_page_ceiling_usd <= 0
            or self.page_limit < 0
            or not 1 <= self.concurrency <= 16
        ):
            raise OcrError("ocr_invalid_policy")
        if self.enabled and (
            self.approved_by is None or self.page_limit < 1 or self.budget_usd <= 0
        ):
            raise OcrError("ocr_invalid_policy")


class OcrLedger(Protocol):
    @property
    def tenant_id(self) -> UUID: ...
    async def policy(self) -> OcrPolicy: ...
    async def cached(self, digest: str, engine: str) -> OcrResult | None: ...
    async def begin(self, digest: str, engine: str, page: int, policy: OcrPolicy) -> None: ...
    async def finish(
        self, digest: str, engine: str, result: OcrResult | None, error: str | None
    ) -> None: ...
