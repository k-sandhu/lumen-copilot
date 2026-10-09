"""Compose the canonical audit sink with a mandatory shared tenant budget store.

No model calls or private vendor JSON here. #692 supplies transaction-owned
atomic storage; this capability cannot dispatch without that injected store.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol
from uuid import UUID

from app.core.config import Settings
from app.domain.audit import AuditAction, AuditActor
from app.domain.decisions import DecisionAttempt, DecisionPolicy, DecisionUsage
from app.domain.entities import AuditOutcome
from app.domain.llm import TokenUsage
from app.llm.decisions import DecisionError
from app.services.audit import AuditSink


def decision_policy_from_settings(
    settings: Settings,
    *,
    tenant_id: UUID,
    tenant_enabled: bool = False,
    fallback_structured_outputs: bool = False,
    api_key: str | None = None,
) -> DecisionPolicy:
    """Seed trusted controls; service-resolved tenant overrides use dataclass replace.

    An enabled deployment never enables a tenant implicitly. Only a verified
    model-capability snapshot may grant fallback_structured_outputs.
    """
    return DecisionPolicy(
        tenant_id=tenant_id,
        enabled=settings.decisions_enabled and tenant_enabled,
        model=settings.decisions_model,
        per_call_ceiling_usd=Decimal(str(settings.decisions_per_call_ceiling_usd)),
        budget_usd=Decimal(str(settings.decisions_tenant_budget_usd)),
        fallback_model=settings.decisions_fallback_model or None,
        fallback_structured_outputs=fallback_structured_outputs,
        api_key=api_key,
    )


class TenantDecisionBudgetStore(Protocol):
    """Shared durable ledger: atomic, tenant-bound and idempotent per attempt ID.

    reserve rejects when held ceilings + actual spend + new ceiling exceed the
    policy budget in the store's budget window. settle persists tokens/cost/model
    and replaces a ceiling with known spend; None holds the ceiling for later
    reconciliation. No in-memory production implementation is permitted.
    """

    @property
    def tenant_id(self) -> UUID: ...

    async def reserve(
        self, attempt: DecisionAttempt, ceiling: Decimal, budget: Decimal
    ) -> bool: ...

    async def settle(self, attempt: DecisionAttempt, usage: DecisionUsage | None) -> None: ...


class CanonicalDecisionLedger:
    def __init__(
        self,
        store: TenantDecisionBudgetStore,
        audit: AuditSink,
        *,
        actor: AuditActor,
        resource_id: str,
        request_id: str,
        source_ip: str,
    ) -> None:
        if store.tenant_id != audit.tenant_id:
            raise ValueError("Decision store and audit tenant differ")
        if not resource_id.strip() or not request_id.strip() or not source_ip.strip():
            raise ValueError("Trusted attribution is required")
        self._store = store
        self._audit = audit
        self._actor = actor
        self._resource_id = resource_id
        self._request_id = request_id
        self._source_ip = source_ip

    @property
    def tenant_id(self) -> UUID:
        return self._store.tenant_id

    def _check(self, attempt: DecisionAttempt) -> None:
        if attempt.tenant_id != self.tenant_id:
            raise DecisionError("decision_tenant_mismatch")

    def _metadata(self, attempt: DecisionAttempt) -> dict[str, object]:
        return {
            "attempt_id": str(attempt.id),
            "attempt": attempt.ordinal,
            "method": attempt.method,
            "model": attempt.model,
            "input_fingerprint": attempt.input_fingerprint,
            "option_orders": {name: list(order) for name, order in attempt.option_orders.items()},
            "order_policy": attempt.order_policy,
            "order_seed": attempt.order_seed,
        }

    async def begin(self, attempt: DecisionAttempt, policy: DecisionPolicy) -> None:
        self._check(attempt)
        if policy.tenant_id != self.tenant_id:
            raise DecisionError("decision_tenant_mismatch")
        if not await self._store.reserve(attempt, policy.per_call_ceiling_usd, policy.budget_usd):
            raise DecisionError("decision_budget_exceeded")
        try:
            await self._audit.emit(
                action=AuditAction.MODEL_DECISION_REQUESTED,
                actor=self._actor,
                resource_type="model_decision",
                resource_id=self._resource_id,
                outcome=AuditOutcome.ALLOWED,
                request_id=self._request_id,
                source_ip=self._source_ip,
                metadata=self._metadata(attempt),
                durable=True,
            )
        except Exception:
            # Dispatch has not happened. Failure still propagates; if settlement
            # fails too, the held ceiling is conservative rather than free spend.
            await self._store.settle(
                attempt,
                DecisionUsage(
                    tokens=TokenUsage(),
                    cost_usd=Decimal(0),
                ),
            )
            raise

    async def finish(
        self, attempt: DecisionAttempt, usage: DecisionUsage | None, error: str | None
    ) -> None:
        self._check(attempt)
        await self._store.settle(attempt, usage)
        metadata = self._metadata(attempt)
        metadata.update(
            {
                "error": error,
                "cost_usd": str(usage.cost_usd) if usage else None,
                "input_tokens": usage.tokens.prompt_tokens if usage else None,
                "output_tokens": usage.tokens.completion_tokens if usage else None,
                "reported_model": usage.reported_model if usage else None,
            }
        )
        await self._audit.emit(
            action=AuditAction.MODEL_DECISION_COMPLETED,
            actor=self._actor,
            resource_type="model_decision",
            resource_id=self._resource_id,
            outcome=AuditOutcome.ERROR if error else AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata=metadata,
            durable=True,
        )
