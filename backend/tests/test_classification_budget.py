from uuid import uuid4

import httpx
import pytest

from app.core.config import Settings
from app.llm.decisions import DecisionError, DecisionsGateway
from app.services.classification_controls import ClassificationControls
from tests.test_decisions_gateway import Ledger, policy, questions


async def test_full_request_tokens_include_options_and_instructions_before_dispatch():
    selected = policy(uuid4())
    ledger = Ledger(selected.tenant_id)
    measured = []

    def count(text):
        measured.append(text)
        return 101

    def forbidden(request):
        raise AssertionError("network dispatch forbidden")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        gateway = DecisionsGateway(
            Settings(DECISIONS_ENABLED=True, OPENROUTER_API_KEY="synthetic"),
            ledger=ledger,
            http_client=client,
            token_counter=count,
            max_input_tokens=100,
        )
        with pytest.raises(DecisionError, match="decision_input_budget_exceeded"):
            await gateway.decide("synthetic evidence", questions(), policy=selected)
    assert "Which team owns this?" in measured[0] and "Accounts" in measured[0]
    assert ledger.intents == []


def test_missing_controls_cannot_enable_classification():
    with pytest.raises(ValueError):
        ClassificationControls(enabled=True)
    assert ClassificationControls().enabled is False
