"""Provider naming is reversible without changing policy or MCP identities."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import litellm
import pytest

from app.domain.llm import ChatMessage, Role, ToolCall, ToolSpec
from app.llm.gateway import LLMGateway
from tests.test_llm_gateway import _settings


async def test_mcp_wire_names_round_trip_definitions_history_and_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    names = ["mcp:srv-a:read.file", "mcp:srv-a:read-file", "mcp:srv-b:" + "long" * 30]
    specs = [ToolSpec(name=n, description="read", parameters={"type": "object"}) for n in names]

    async def fake_completion(**kwargs: Any) -> Any:
        captured.update(kwargs)

        async def chunks() -> Any:
            fragments = [
                SimpleNamespace(
                    index=i,
                    id=f"call{i}",
                    function=SimpleNamespace(name=t["function"]["name"], arguments="{}"),
                )
                for i, t in enumerate(kwargs["tools"])
            ]
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, tool_calls=fragments),
                        finish_reason="tool_calls",
                    )
                ],
                usage=None,
            )

        return chunks()

    monkeypatch.setattr(litellm, "acompletion", fake_completion)
    history = [
        ChatMessage(role=Role.USER, content="read"),
        ChatMessage(
            role=Role.ASSISTANT,
            content="",
            tool_calls=(ToolCall(id="old", name=names[0], arguments={}),),
        ),
        ChatMessage(role=Role.TOOL, content="evidence", name=names[0], tool_call_id="old"),
    ]
    events = [ev async for ev in LLMGateway(_settings()).stream_tools(history, tools=specs)]
    wire = [t["function"]["name"] for t in captured["tools"]]
    assert all(re.fullmatch(r"[A-Za-z0-9_]{1,64}", n) for n in wire)
    assert len(set(wire)) == len(names)
    assert all(n.startswith("mcp__") for n in wire)
    assert captured["messages"][1]["tool_calls"][0]["function"]["name"] == wire[0]
    assert captured["messages"][2]["name"] == wire[0]
    assert [c.name for c in events[-1].tool_calls] == names
    assert [s.name for s in specs] == names
    assert history[2].name == names[0]


def test_wire_mapping_is_stable_collision_safe_and_unknown_is_not_resolved() -> None:
    from app.llm.tool_names import ToolNameMap

    originals = ["search_text", "mcp:srv-a:read.file", "mcp:srv-a:read-file"]
    first = ToolNameMap(originals)
    second = ToolNameMap(list(reversed(originals)))
    assert first.to_wire("search_text") == "search_text"
    assert first.to_wire(originals[1]) != first.to_wire(originals[2])
    for original in originals:
        assert first.to_wire(original) == second.to_wire(original)
        assert first.from_wire(first.to_wire(original)) == original
    assert first.from_wire("mcp__unknown") == "mcp__unknown"


def test_wire_mapping_rejects_a_valid_name_shadowing_a_generated_name() -> None:
    from app.core.errors import ValidationError
    from app.llm.tool_names import ToolNameMap

    original = "mcp:srv-a:read.file"
    generated = ToolNameMap([original]).to_wire(original)
    with pytest.raises(ValidationError, match="collision"):
        ToolNameMap([original, generated])
