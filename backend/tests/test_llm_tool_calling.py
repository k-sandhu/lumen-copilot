"""Gateway tool-calling tests — ``stream_tools`` (CC-6 #24).

LiteLLM is monkeypatched (no network) to assert the tool-aware streaming
contract the chat runtime depends on:

* text fragments are yielded as ``StreamEvent(text=...)`` in order;
* tool-call fragments streamed across chunks are accumulated (name once,
  arguments in pieces) into one parsed ``ToolCall`` on a terminal event;
* the terminal event carries ``finish_reason`` (+ usage when reported);
* the request renders the tools + tool/assistant messages into the vendor wire
  shape, with model id / timeout / creds sourced from config;
* a blank key raises a typed ``DependencyError`` (never a hang);
* domain types only cross the boundary (no LiteLLM object).
"""

from __future__ import annotations

from contextlib import aclosing
from typing import Any

import litellm
import pytest

from app.core.config import Settings
from app.core.errors import DependencyError
from app.domain.llm import ChatMessage, Role, StreamEvent, ToolCall, ToolSpec
from app.llm.gateway import LLMGateway

_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:5432/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "CELERY_BROKER_URL": "redis://localhost:6379/1",
    "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_ACCESS_KEY": "test",
    "S3_SECRET_KEY": "test_secret",
    "S3_BUCKET": "test-bucket",
}


def _settings(**overrides: str) -> Settings:
    values: dict[str, Any] = {
        **_BASE_ENV,
        "OPENROUTER_API_KEY": "sk-test-key",
        "LLM_MODEL": "openrouter/openai/gpt-4o-mini",
        "LLM_TIMEOUT_SECONDS": "60",
        **overrides,
    }
    return Settings(**values)  # type: ignore[arg-type]


_TOOLS = (
    ToolSpec(
        name="search_text",
        description="search",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    ),
)


# --- Vendor-shaped streaming fakes (NOT domain types) ----------------------


class _FnDelta:
    def __init__(self, name: str | None, arguments: str | None) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCallFrag:
    def __init__(
        self, *, index: int, id: str | None, name: str | None, arguments: str | None
    ) -> None:
        self.index = index
        self.id = id
        self.function = _FnDelta(name, arguments)


class _Delta:
    def __init__(
        self, content: str | None = None, tool_calls: list[_ToolCallFrag] | None = None
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta: _Delta, finish_reason: str | None = None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason


class _Usage:
    def __init__(self, prompt: int, completion: int, total: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total


class _Part:
    def __init__(
        self,
        *,
        content: str | None = None,
        tool_calls: list[_ToolCallFrag] | None = None,
        finish_reason: str | None = None,
        usage: _Usage | None = None,
    ) -> None:
        self.choices = [_Choice(_Delta(content, tool_calls), finish_reason)]
        self.usage = usage


class _FakeStream:
    def __init__(self, parts: list[_Part]) -> None:
        self._parts = parts
        self.closed = False

    def __aiter__(self) -> _FakeStream:
        self._it = iter(self._parts)
        return self

    async def __anext__(self) -> _Part:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed = True


# --- Tests -----------------------------------------------------------------


async def test_stream_tools_yields_text_then_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    parts = [
        _Part(content="Hello"),
        _Part(content=", world"),
        _Part(finish_reason="stop", usage=_Usage(10, 4, 14)),
    ]

    async def fake_acompletion(**kwargs: Any) -> _FakeStream:
        return _FakeStream(parts)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    events: list[StreamEvent] = []
    async with aclosing(
        gw.stream_tools([ChatMessage(role=Role.USER, content="hi")], tools=_TOOLS)
    ) as stream:
        async for ev in stream:
            events.append(ev)

    texts = [e.text for e in events if e.text]
    assert texts == ["Hello", ", world"]
    terminal = events[-1]
    assert terminal.finish_reason == "stop"
    assert terminal.tool_calls == ()
    assert terminal.usage is not None
    assert terminal.usage.total_tokens == 14


async def test_stream_tools_accumulates_fragmented_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Name + id arrive once; arguments arrive across three fragments.
    parts = [
        _Part(
            tool_calls=[_ToolCallFrag(index=0, id="call_1", name="search_text", arguments='{"qu')]
        ),
        _Part(tool_calls=[_ToolCallFrag(index=0, id=None, name=None, arguments='ery": "ta')]),
        _Part(tool_calls=[_ToolCallFrag(index=0, id=None, name=None, arguments='xes"}')]),
        _Part(finish_reason="tool_calls"),
    ]

    async def fake_acompletion(**kwargs: Any) -> _FakeStream:
        return _FakeStream(parts)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    events = [
        ev async for ev in gw.stream_tools([ChatMessage(role=Role.USER, content="q")], tools=_TOOLS)
    ]
    terminal = events[-1]
    assert terminal.finish_reason == "tool_calls"
    assert terminal.tool_calls == (
        ToolCall(id="call_1", name="search_text", arguments={"query": "taxes"}),
    )


async def test_stream_tools_renders_tools_and_messages_to_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeStream:
        captured.update(kwargs)
        return _FakeStream([_Part(content="ok", finish_reason="stop")])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    messages = [
        ChatMessage(role=Role.SYSTEM, content="sys"),
        ChatMessage(role=Role.USER, content="q"),
        ChatMessage(
            role=Role.ASSISTANT,
            content="",
            tool_calls=(ToolCall(id="c1", name="search_text", arguments={"query": "x"}),),
        ),
        ChatMessage(role=Role.TOOL, content="result", tool_call_id="c1", name="search_text"),
    ]
    _ = [ev async for ev in gw.stream_tools(messages, tools=_TOOLS)]

    assert captured["model"] == "openrouter/openai/gpt-4o-mini"
    assert captured["timeout"] == 60.0
    assert captured["api_key"] == "sk-test-key"
    assert captured["stream"] is True
    # Tools rendered into the OpenAI function-tool array.
    assert captured["tools"][0]["type"] == "function"
    assert captured["tools"][0]["function"]["name"] == "search_text"
    # The assistant tool-call turn rendered tool_calls; the tool turn its id.
    wire = captured["messages"]
    assert wire[2]["tool_calls"][0]["function"]["name"] == "search_text"
    assert wire[3]["tool_call_id"] == "c1"
    assert wire[3]["role"] == "tool"


async def test_stream_tools_without_key_raises_dependency_error() -> None:
    gw = LLMGateway(_settings(OPENROUTER_API_KEY=""))
    with pytest.raises(DependencyError):
        async for _ in gw.stream_tools([ChatMessage(role=Role.USER, content="q")], tools=_TOOLS):
            pass


async def test_stream_tools_malformed_arguments_yield_empty_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts = [
        _Part(
            tool_calls=[_ToolCallFrag(index=0, id="c", name="search_text", arguments="not-json")]
        ),
        _Part(finish_reason="tool_calls"),
    ]

    async def fake_acompletion(**kwargs: Any) -> _FakeStream:
        return _FakeStream(parts)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    events = [
        ev async for ev in gw.stream_tools([ChatMessage(role=Role.USER, content="q")], tools=_TOOLS)
    ]
    assert events[-1].tool_calls[0].arguments == {}
