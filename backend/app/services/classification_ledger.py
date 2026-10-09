"""Shared tenant budget transactions; no process-local spend counters."""

from decimal import Decimal
from uuid import UUID

from app.db.classification import ClassificationRepository, DecisionBudgetRepository
from app.db.repositories import LlmProviderRepository
from app.db.session import tenant_session_scope
from app.domain.classification import ClassificationWork
from app.domain.decisions import DecisionAttempt, DecisionUsage
from app.services.provider_models import is_provider_model_id, resolve_provider_model


class DurableDecisionBudgetStore:
    def __init__(self, tenant_id: UUID, *, work: ClassificationWork | None = None) -> None:
        self._tenant = tenant_id
        if work is not None and work.tenant_id != tenant_id:
            raise ValueError("classification ledger tenant mismatch")
        self._work = work

    @property
    def tenant_id(self) -> UUID:
        return self._tenant

    async def reserve(self, attempt: DecisionAttempt, ceiling: Decimal, budget: Decimal) -> bool:
        async with tenant_session_scope(self._tenant) as session:
            await DecisionBudgetRepository(session, self._tenant).lock_tenant()
            if self._work is not None:
                current = await ClassificationRepository(session, self._tenant).get(
                    self._work.document_id
                )
                if (
                    current is None
                    or current.override_path
                    or current.revision != self._work.revision
                    or current.input_fingerprint != self._work.input_fingerprint
                    or current.run_id != self._work.run_id
                ):
                    return False
            controls = await ClassificationRepository(session, self._tenant).policy()
            allowed: set[str] = set()
            for identifier in controls.get("allowed_models", []):
                if is_provider_model_id(identifier):
                    resolved = await resolve_provider_model(
                        identifier, LlmProviderRepository(session, self._tenant)
                    )
                    if resolved is not None:
                        allowed.add(resolved.raw_model_id)
                else:
                    allowed.add(identifier)
            if (
                not controls.get("enabled")
                or not controls.get("provider_approved")
                or attempt.model not in allowed
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
