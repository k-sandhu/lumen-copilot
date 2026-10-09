"""Shared tenant budget transactions; no process-local spend counters."""

from decimal import Decimal
from uuid import UUID

from app.db.classification import ClassificationRepository, DecisionBudgetRepository
from app.db.session import tenant_session_scope
from app.domain.decisions import DecisionAttempt, DecisionUsage


class DurableDecisionBudgetStore:
    def __init__(self, tenant_id: UUID) -> None:
        self._tenant = tenant_id

    @property
    def tenant_id(self) -> UUID:
        return self._tenant

    async def reserve(self, attempt: DecisionAttempt, ceiling: Decimal, budget: Decimal) -> bool:
        async with tenant_session_scope(self._tenant) as session:
            controls = await ClassificationRepository(session, self._tenant).policy()
            if (
                not controls.get("enabled")
                or not controls.get("provider_approved")
                or attempt.model not in controls.get("allowed_models", [])
            ):
                return False
            approved_ceiling = Decimal(str(controls.get("per_call_ceiling_usd", "0")))
            approved_budget = Decimal(str(controls.get("budget_usd", "0")))
            if ceiling > approved_ceiling or budget > approved_budget:
                return False
            return await DecisionBudgetRepository(session, self._tenant).reserve(
                attempt, ceiling, budget
            )

    async def settle(self, attempt: DecisionAttempt, usage: DecisionUsage | None) -> None:
        async with tenant_session_scope(self._tenant) as session:
            await DecisionBudgetRepository(session, self._tenant).settle(attempt, usage)
