"""Actual LiteLLM boundary uses enforced strict schema without unbudgeted retries."""

import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings
from app.domain.llm import ChatMessage, Role
from app.llm.gateway import LLMGateway, LlmProviderError


async def test_actual_gateway_sends_strict_schema_and_reads_cost(monkeypatch):
    response = SimpleNamespace(
        model="synthetic-reported-model",
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, cost=0.001),
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content='{"probability":0.5}', refusal=None),
            )
        ],
    )
    call = AsyncMock(return_value=response)
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=call))
    # Tenant credential override works even with an unconfigured process key.
    gateway = LLMGateway(Settings(OPENROUTER_API_KEY=""))
    result = await gateway.structured_chat(
        [ChatMessage(Role.USER, "Synthetic evidence")],
        schema={"type": "object"},
        model="openrouter/openai/synthetic",
        api_key="synthetic-key",
        timeout_seconds=1,
        max_tokens=100,
    )
    args = call.call_args.kwargs
    assert args["response_format"]["json_schema"]["strict"] is True
    assert args["provider"] == {"require_parameters": True}
    assert args["num_retries"] == 0
    assert args["max_tokens"] == 100
    assert args["timeout"] == 1
    assert result.cost_usd == Decimal("0.001")
    assert result.model == "synthetic-reported-model"


@pytest.mark.parametrize("fault", ["missing_cost", "bad_tokens", "truncated", "refused"])
async def test_missing_accounting_truncation_and_refusal_fail_closed(monkeypatch, fault):
    response = SimpleNamespace(
        model="synthetic",
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, cost=0.001),
        choices=[
            SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content="{}", refusal=None)
            )
        ],
    )
    if fault == "missing_cost":
        del response.usage.cost
    if fault == "bad_tokens":
        response.usage.prompt_tokens = -1
    if fault == "truncated":
        response.choices[0].finish_reason = "length"
    if fault == "refused":
        response.choices[0].message.refusal = "refused"
    monkeypatch.setitem(
        sys.modules, "litellm", SimpleNamespace(acompletion=AsyncMock(return_value=response))
    )
    with pytest.raises(LlmProviderError):
        await LLMGateway(Settings(OPENROUTER_API_KEY="synthetic-key")).structured_chat(
            [], schema={}, model="openrouter/openai/synthetic", max_tokens=100
        )
