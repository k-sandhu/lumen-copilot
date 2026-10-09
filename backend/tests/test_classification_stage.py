from decimal import Decimal
from uuid import uuid4

import pytest

from app.classification.taxonomy import load_taxonomy
from app.domain.classification_features import ClassificationFeatures
from app.domain.decisions import (
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionPolicy,
    DecisionResult,
    DecisionUsage,
    PredicateAnswer,
)
from app.domain.llm import TokenUsage
from app.llm.decisions import DecisionError
from app.services.classification import classify


class Gateway:
    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    async def decide(self, state, questions, *, policy):
        self.calls.append(questions)
        if len(self.calls) == self.fail_at:
            raise DecisionError("decision_timeout", retryable=True)
        answers = {}
        for q in questions:
            if isinstance(q, ChoiceQuestion):
                chosen = ["financial", "financial/transactions", "financial/transactions/invoice"][
                    len(self.calls) - 1
                ]
                answers[q.name] = ChoiceAnswer(
                    chosen, {o.value: float(o.value == chosen) for o in q.options}, 0.9
                )
            else:
                answers[q.name] = PredicateAnswer(0.8)
        return DecisionResult(
            answers,
            DecisionUsage(TokenUsage(10, 2, 12), Decimal(".001"), "fixture"),
            "fixture",
            "decisions",
            (),
            Decimal(".001"),
        )


def evidence(**values):
    import json

    return ClassificationFeatures.from_json(
        json.dumps(
            {
                "schema_version": 1,
                "taxonomy_version": "1.0.0",
                "rules_version": "1",
                "excerpts": [
                    {"text": "Synthetic invoice", "block_id": "b", "char_start": 0, "char_end": 17}
                ],
                "rule_path": None,
                "rule_ids": [],
                **values,
            }
        )
    )


def policy(tenant):
    return DecisionPolicy(tenant, True, "fixture", Decimal(".01"), Decimal(".05"))


async def test_selected_branch_cascade_and_one_facet_request():
    tenant = uuid4()
    gateway = Gateway()
    result = await classify(
        evidence(), load_taxonomy(), gateway, tenant_id=tenant, policy=policy(tenant)
    )
    assert result["status"] == "classified"
    assert result["path"] == "financial/transactions/invoice"
    assert len(result["levels"]) == 3
    assert len(gateway.calls) == 3
    assert all(o.value.startswith("financial/") for o in gateway.calls[1][0].options)
    assert len(gateway.calls[2]) == 1 + len(load_taxonomy()["facets"])
    assert result["review_required"] is True
    assert result["total_cost_usd"] == "0.003"


async def test_provider_failure_retains_completed_parent_and_unknown_cost():
    tenant = uuid4()
    gateway = Gateway(fail_at=2)
    result = await classify(
        evidence(), load_taxonomy(), gateway, tenant_id=tenant, policy=policy(tenant)
    )
    assert result["status"] == "incomplete"
    assert result["path"] == "financial"
    assert result["reason"] == "decision_timeout"
    assert result["retryable"] is True
    assert result["total_cost_usd"] is None


async def test_foreign_tenant_policy_never_dispatches():
    gateway = Gateway()
    with pytest.raises(ValueError):
        await classify(
            evidence(), load_taxonomy(), gateway, tenant_id=uuid4(), policy=policy(uuid4())
        )
    assert gateway.calls == []


async def test_deterministic_rule_never_calls_model():
    tenant = uuid4()
    gateway = Gateway()
    result = await classify(
        evidence(rule_path="safety/hazards/safety_data_sheet", rule_ids=["fixture"]),
        load_taxonomy(),
        gateway,
        tenant_id=tenant,
        policy=policy(tenant),
    )
    assert result["method"] == "rules"
    assert result["facets"]["signed"]["value"] is None
    assert gateway.calls == []
