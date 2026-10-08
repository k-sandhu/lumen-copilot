"""Vendor-free constrained decisions and mandatory accounting boundary (ADR-0027)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from app.domain.llm import TokenUsage


@dataclass(frozen=True, slots=True)
class DecisionOption:
    value: str
    description: str


@dataclass(frozen=True, slots=True)
class ChoiceQuestion:
    name: str
    instructions: str
    options: tuple[DecisionOption, ...]


@dataclass(frozen=True, slots=True)
class PredicateQuestion:
    name: str
    instructions: str


@dataclass(frozen=True, slots=True)
class ScoreQuestion:
    name: str
    instructions: str
    levels: tuple[str, ...]


DecisionQuestion = ChoiceQuestion | PredicateQuestion | ScoreQuestion


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float


@dataclass(frozen=True, slots=True)
class PredicateAnswer:
    probability: float


@dataclass(frozen=True, slots=True)
class ScoreAnswer:
    score: float
    probabilities: Mapping[str, float]
    confidence: float


DecisionAnswer = ChoiceAnswer | PredicateAnswer | ScoreAnswer


@dataclass(frozen=True, slots=True)
class DecisionUsage:
    tokens: TokenUsage
    cost_usd: Decimal
    reported_model: str | None = None


@dataclass(frozen=True, slots=True)
class DecisionPolicy:
    """Trusted service-resolved tenant controls, never populated from an API body.

    Budget and ceiling apply to each dispatch including retries/fallback. Budget
    window/atomic storage belong to the injected ledger. Credentials are ephemeral.
    """

    tenant_id: UUID
    enabled: bool
    model: str
    per_call_ceiling_usd: Decimal
    budget_usd: Decimal
    fallback_model: str | None = None
    fallback_structured_outputs: bool = False
    api_key: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class DecisionAttempt:
    id: UUID
    tenant_id: UUID
    ordinal: int
    method: str
    model: str
    input_fingerprint: str
    option_orders: Mapping[str, tuple[str, ...]]
    order_policy: str
    order_seed: int


@dataclass(frozen=True, slots=True)
class DecisionResult:
    answers: Mapping[str, DecisionAnswer]
    usage: DecisionUsage
    model: str
    method: str
    attempts: tuple[DecisionAttempt, ...]
    total_cost_usd: Decimal | None


class DecisionLedger(Protocol):
    """Tenant-bound shared budget/usage ledger with canonical durable audit.

    begin reserves a ceiling atomically AND commits intent before dispatch.
    finish records usage/error and commits terminal audit before a result leaves.
    Unknown usage retains its reservation. Neither method may silently no-op.
    """

    @property
    def tenant_id(self) -> UUID: ...

    async def begin(self, attempt: DecisionAttempt, policy: DecisionPolicy) -> None: ...

    async def finish(
        self, attempt: DecisionAttempt, usage: DecisionUsage | None, error: str | None
    ) -> None: ...
