"""INV-6: no model dispatch/result without canonical intent/terminal audit."""

from decimal import Decimal
from uuid import uuid4

import pytest

from app.domain.decisions import DecisionAttempt, DecisionPolicy, DecisionUsage
from app.domain.llm import TokenUsage
from app.llm.decisions import DecisionError
from app.services.decisions_accounting import CanonicalDecisionLedger


class Store:
    def __init__(self, tenant_id):
        self.tenant_id = tenant_id
        self.reservations = {}
        self.usage = {}

    async def reserve(self, attempt, ceiling, budget):
        if sum(self.reservations.values(), Decimal(0)) + ceiling > budget:
            return False
        self.reservations[attempt.id] = ceiling
        return True

    async def settle(self, attempt, usage):
        self.usage[attempt.id] = usage
        if usage is not None:
            self.reservations[attempt.id] = usage.cost_usd


class Sink:
    def __init__(self, tenant_id):
        self.tenant_id = tenant_id
        self.events = []
        self.fail = False

    async def emit(self, **event):
        if self.fail:
            raise RuntimeError("sink unavailable")
        assert event["durable"] is True
        self.events.append(event)


def build():
    from app.domain.audit import AuditActor

    tenant = uuid4()
    store = Store(tenant)
    sink = Sink(tenant)
    ledger = CanonicalDecisionLedger(
        store,
        sink,
        actor=AuditActor.system(),
        resource_id="synthetic-document",
        request_id="synthetic-request",
        source_ip="system",
    )
    attempt = DecisionAttempt(
        uuid4(),
        tenant,
        1,
        "decisions",
        "synthetic/model",
        "abc123",
        {"type": ("invoice", "other")},
        "fixed",
        0,
    )
    policy = DecisionPolicy(tenant, True, "synthetic/model", Decimal("0.01"), Decimal("0.02"))
    return ledger, store, sink, attempt, policy


async def test_canonical_ledger_reserves_records_and_audits():
    ledger, store, sink, attempt, policy = build()
    await ledger.begin(attempt, policy)
    usage = DecisionUsage(TokenUsage(10, 1, 11), Decimal("0.001"))
    await ledger.finish(attempt, usage, None)
    assert store.usage[attempt.id] == usage
    assert store.reservations[attempt.id] == Decimal("0.001")
    assert [event["action"].value for event in sink.events] == [
        "model.decision_requested",
        "model.decision_completed",
    ]
    assert sink.events[-1]["metadata"]["cost_usd"] == "0.001"
    assert sink.events[-1]["metadata"]["input_tokens"] == 10
    assert "evidence" not in str(sink.events)


async def test_unknown_spend_retains_reservation_and_exhaustion_denies():
    from dataclasses import replace

    ledger, store, sink, attempt, policy = build()
    await ledger.begin(attempt, policy)
    await ledger.finish(attempt, None, "decision_timeout")
    assert store.reservations[attempt.id] == Decimal("0.01")
    second = replace(attempt, id=uuid4(), ordinal=2)
    await ledger.begin(second, policy)
    with pytest.raises(DecisionError, match="decision_budget_exceeded"):
        await ledger.begin(replace(attempt, id=uuid4(), ordinal=3), policy)
    assert sink.events[1]["metadata"]["cost_usd"] is None


async def test_missing_audit_fails_and_never_returns_success():
    ledger, store, sink, attempt, policy = build()
    sink.fail = True
    with pytest.raises(RuntimeError):
        await ledger.begin(attempt, policy)
    assert store.reservations[attempt.id] == Decimal(0)  # not dispatched
    sink.fail = False
    await ledger.begin(attempt, policy)
    sink.fail = True
    with pytest.raises(RuntimeError):
        await ledger.finish(attempt, DecisionUsage(TokenUsage(), Decimal("0.001")), None)


def test_foreign_tenant_store_and_sink_are_rejected():
    from app.domain.audit import AuditActor

    with pytest.raises(ValueError):
        CanonicalDecisionLedger(
            Store(uuid4()),
            Sink(uuid4()),
            actor=AuditActor.system(),
            resource_id="d",
            request_id="r",
            source_ip="system",
        )


def test_deployment_defaults_seed_controls_without_enabling_a_tenant():
    from app.core.config import Settings
    from app.services.decisions_accounting import decision_policy_from_settings

    settings = Settings(
        DECISIONS_ENABLED=True,
        DECISIONS_MODEL="synthetic/decision",
        DECISIONS_FALLBACK_MODEL="openrouter/synthetic/chat",
        DECISIONS_TENANT_BUDGET_USD=1.25,
        DECISIONS_PER_CALL_CEILING_USD=0.01,
    )
    tenant = uuid4()
    selected = decision_policy_from_settings(settings, tenant_id=tenant)
    assert selected.enabled is False
    assert selected.model == "synthetic/decision"
    assert selected.fallback_model == "openrouter/synthetic/chat"
    assert selected.budget_usd == Decimal("1.25")
    assert selected.per_call_ceiling_usd == Decimal("0.01")
    assert not selected.fallback_structured_outputs
