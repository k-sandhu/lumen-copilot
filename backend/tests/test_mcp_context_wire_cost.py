"""Context budgets must use MCP names after provider-safe name mapping."""

from __future__ import annotations

import json
from typing import Any

import litellm
import pytest

from app.core.errors import ValidationError
from app.domain.llm import ChatMessage, Role, ToolCall, ToolSpec
from app.llm.context import ContextConfig, assemble_context, estimate_message_tokens, fit_transcript
from app.llm.gateway import LLMGateway
from app.llm.tool_names import ToolNameMap
from tests.test_llm_gateway import _settings


def _byte_counter(text: str) -> int:
    return len(text.encode("utf-8"))


def _wire_cost(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    """Count the exact JSON payload bytes plus the context module's array framing."""
    return (
        _byte_counter(json.dumps(tools, sort_keys=True))
        + 2
        + sum(_byte_counter(json.dumps(message, sort_keys=True)) + 4 for message in messages)
    )


async def _capture_gateway_wire(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[ChatMessage],
    tools: list[ToolSpec],
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_completion(**kwargs: Any) -> Any:
        captured.update(kwargs)

        async def no_chunks() -> Any:
            if False:
                yield None

        return no_chunks()

    monkeypatch.setattr(litellm, "acompletion", fake_completion)
    gateway = LLMGateway(_settings())
    _ = [
        event
        async for event in gateway.stream_tools(
            messages,
            tools=tools,
            model="test/model",
        )
    ]
    return captured


def _tool_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="mcp:human-resources:read",
            description="Read current employee records.",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        ),
        ToolSpec(
            name="search_text",
            description="Search accessible passages.",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        ),
    ]


@pytest.mark.asyncio
async def test_estimate_covers_gateway_mapped_mcp_tools_and_historical_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = "mcp:human-resources:read"
    historical = "mcp:legacy-hr:read.document"
    tools = _tool_specs()
    messages = [
        ChatMessage(role=Role.USER, content="Find the policy."),
        ChatMessage(
            role=Role.ASSISTANT,
            content="",
            tool_calls=(ToolCall(id="prior-call", name=historical, arguments={"query": "policy"}),),
        ),
        ChatMessage(
            role=Role.TOOL,
            content="Prior result.",
            name=historical,
            tool_call_id="prior-call",
        ),
    ]
    original_names = [tool.name for tool in tools]
    original_call_name = messages[1].tool_calls[0].name
    original_result_name = messages[2].name

    captured = await _capture_gateway_wire(monkeypatch, messages, tools)
    actual_tools = captured["tools"]
    actual_messages = captured["messages"]
    expected_map = ToolNameMap([current, "search_text", historical])
    wire_current = expected_map.to_wire(current)
    wire_historical = expected_map.to_wire(historical)

    assert [tool["function"]["name"] for tool in actual_tools] == [wire_current, "search_text"]
    assert actual_messages[1]["tool_calls"][0]["function"]["name"] == wire_historical
    assert actual_messages[2]["name"] == wire_historical
    assert all(len(tool["function"]["name"]) <= 64 for tool in actual_tools)
    assert len(actual_messages[1]["tool_calls"][0]["function"]["name"]) <= 64
    assert len(actual_messages[2]["name"]) <= 64

    estimate = estimate_message_tokens(messages, tools, counter=_byte_counter)
    actual = _wire_cost(actual_messages, actual_tools)
    assert estimate >= actual, f"context estimate {estimate} undercounts mapped wire {actual}"

    # Mapping is a provider-boundary copy; product identities remain untouched.
    assert [tool.name for tool in tools] == original_names
    assert messages[1].tool_calls[0].name == original_call_name == historical
    assert messages[2].name == original_result_name == historical


@pytest.mark.asyncio
async def test_tight_fixed_context_budget_rejects_mapped_mcp_tool_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = [_tool_specs()[0]]
    system_prompt = "Grounded answers only."
    question = "fact"
    captured = await _capture_gateway_wire(
        monkeypatch,
        [ChatMessage(role=Role.USER, content=question)],
        tools,
    )

    prompt_messages = [
        ChatMessage(role=Role.SYSTEM, content=system_prompt),
        ChatMessage(role=Role.USER, content=question),
    ]
    actual_wire_messages = LLMGateway._to_wire_messages(prompt_messages)
    actual_wire_cost = _wire_cost(actual_wire_messages, captured["tools"])

    # Independently compute the pre-fix internal-name cost so the window is
    # deliberately between the costs, regardless of incidental schema bytes.
    internal_wire_tools = LLMGateway._to_wire_tools(tools)
    internal_cost = _wire_cost(actual_wire_messages, internal_wire_tools)
    assert actual_wire_cost > internal_cost
    budget = (actual_wire_cost + internal_cost) // 2
    assert internal_cost <= budget < actual_wire_cost

    config = ContextConfig(fallback_max_input_tokens=budget + 1024, output_headroom_tokens=0)
    with pytest.raises(ValidationError) as rejected:
        assemble_context(
            model="unknown/test-model",
            system_prompt=system_prompt,
            history=[],
            question=question,
            tools=tools,
            config=config,
            counter=_byte_counter,
            max_input_resolver=lambda _model: None,
        )
    assert getattr(rejected.value, "code", None) == "context_too_large"
    with pytest.raises(ValidationError) as transcript_rejected:
        fit_transcript(
            prompt_messages,
            model="unknown/test-model",
            tools=tools,
            config=config,
            counter=_byte_counter,
            max_input_resolver=lambda _model: None,
        )
    assert getattr(transcript_rejected.value, "code", None) == "context_too_large"

    # Safe built-ins do not grow under the mapping policy.
    safe_spec = _tool_specs()[1]
    safe_map = ToolNameMap([safe_spec.name])
    assert safe_map.to_wire(safe_spec.name) == safe_spec.name == "search_text"
