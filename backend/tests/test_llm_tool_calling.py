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


async def test_first_tool_fragment_emits_the_classification_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0016 §6 (#414, #447 blocker 1): the REAL gateway emits ONE
    provider-neutral ``tool_call_started`` event at the FIRST tool-call
    fragment — between the text that precedes it and the text that follows —
    so the runtime can flush narration and close the retry window mid-stream,
    not at stream end."""
    parts = [
        _Part(content="Let me look. "),
        _Part(
            tool_calls=[_ToolCallFrag(index=0, id="call_1", name="search_text", arguments='{"qu')]
        ),
        _Part(content="Checking now… "),
        _Part(tool_calls=[_ToolCallFrag(index=0, id=None, name=None, arguments='ery": "x"}')]),
        _Part(finish_reason="tool_calls"),
    ]

    async def fake_acompletion(**kwargs: Any) -> _FakeStream:
        return _FakeStream(parts)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())
    events = [
        ev async for ev in gw.stream_tools([ChatMessage(role=Role.USER, content="q")], tools=_TOOLS)
    ]
    kinds = [
        "signal" if e.tool_call_started else ("text" if e.text else "terminal")
        for e in events
    ]
    # Exactly one signal, after the first text and before the second.
    assert kinds == ["text", "signal", "text", "terminal"]
    # The second fragment did NOT re-signal; the assembled call still arrives.
    assert events[-1].tool_calls and events[-1].tool_calls[0].name == "search_text"


# --- Prompt-cache directives (ADR-0016 §2, #411) ----------------------------
#
# The gateway decorates the OUTGOING wire per provider family — Anthropic-style
# ``cache_control`` breakpoints on the serialized messages, OpenAI-style
# ``prompt_cache_key`` routing affinity via ``extra_body`` — and sends the exact
# pre-#411 wire for unknown families or when the kill-switch is off (AC-3).
# These capture ``litellm.acompletion``'s kwargs and assert the wire byte-shape.


def _capture_acompletion(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeStream:
        captured.clear()
        captured.update(kwargs)
        return _FakeStream([_Part(content="ok", finish_reason="stop")])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    return captured


def _has_cache_control(wire_message: dict[str, Any]) -> bool:
    content = wire_message.get("content")
    return isinstance(content, list) and any(
        isinstance(part, dict) and "cache_control" in part for part in content
    )


_CONVO = [
    ChatMessage(role=Role.SYSTEM, content="sys prompt"),
    ChatMessage(role=Role.USER, content="first question"),
    ChatMessage(role=Role.ASSISTANT, content="first answer"),
    ChatMessage(role=Role.USER, content="follow-up"),
]


async def test_anthropic_family_marks_prefix_and_last_block_breakpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anthropic-family models get exactly two ``cache_control`` breakpoints:
    message[0] (the stable system prefix boundary) and the LAST message (the
    moving mark — this call WRITES the full prefix; the next append-only call
    READS it). Other messages stay plain strings, and no ``extra_body`` is
    sent (that's OpenAI's knob)."""
    captured = _capture_acompletion(monkeypatch)
    gw = LLMGateway(_settings(LLM_MODEL="openrouter/anthropic/claude-opus-4.8"))

    _ = [ev async for ev in gw.stream_tools(_CONVO, tools=_TOOLS, cache_key="sess-1")]

    wire = captured["messages"]
    assert [_has_cache_control(m) for m in wire] == [True, False, False, True]
    # The decorated entries are single text parts wrapping the original string.
    part = wire[0]["content"][0]
    assert part == {
        "type": "text",
        "text": "sys prompt",
        "cache_control": {"type": "ephemeral"},
    }
    assert wire[3]["content"][0]["text"] == "follow-up"
    # Undecorated entries keep the plain-string shape.
    assert wire[1]["content"] == "first question"
    assert wire[2]["content"] == "first answer"
    assert "extra_body" not in captured


async def test_anthropic_single_message_gets_one_breakpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-message transcript anchors only message[0] — the len-2 index would
    alias the same message; the anchor set dedupes it (no double-wrap)."""
    captured = _capture_acompletion(monkeypatch)
    gw = LLMGateway(_settings(LLM_MODEL="openrouter/anthropic/claude-opus-4.8"))
    _ = [
        ev
        async for ev in gw.stream_tools(
            [ChatMessage(role=Role.USER, content="q")], tools=_TOOLS
        )
    ]
    wire = captured["messages"]
    assert len(wire) == 1 and _has_cache_control(wire[0])
    assert wire[0]["content"] == [
        {"type": "text", "text": "q", "cache_control": {"type": "ephemeral"}}
    ]


async def test_anthropic_moving_mark_lands_on_latest_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime's real tool-loop shape — the synthesis call after one tool:
    ``[system, question, assistant(tool_calls, content=""), tool(result)]``.
    The moving mark must land on the LATEST TOOL RESULT (the big block worth
    writing so the next call — or the next answer — reads it), never one block
    early. The empty tool-call turn itself is unwrappable and stays intact."""
    captured = _capture_acompletion(monkeypatch)
    gw = LLMGateway(_settings(LLM_MODEL="openrouter/anthropic/claude-opus-4.8"))
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
    wire = captured["messages"]
    assert [_has_cache_control(m) for m in wire] == [True, False, False, True]
    assert wire[3]["content"][0]["text"] == "result"
    assert wire[3]["role"] == "tool" and wire[3]["tool_call_id"] == "c1"
    assert wire[2]["content"] == ""
    assert wire[2]["tool_calls"][0]["function"]["name"] == "search_text"

    # Parallel batch (#412): the mark is on the LAST of the batch's results.
    batch = [
        *messages[:2],
        ChatMessage(
            role=Role.ASSISTANT,
            content="",
            tool_calls=(
                ToolCall(id="c1", name="search_text", arguments={"query": "x"}),
                ToolCall(id="c2", name="search_text", arguments={"query": "y"}),
            ),
        ),
        ChatMessage(role=Role.TOOL, content="r1", tool_call_id="c1", name="search_text"),
        ChatMessage(role=Role.TOOL, content="r2", tool_call_id="c2", name="search_text"),
    ]
    _ = [ev async for ev in gw.stream_tools(batch, tools=_TOOLS)]
    wire = captured["messages"]
    assert [_has_cache_control(m) for m in wire] == [True, False, False, False, True]
    assert wire[4]["content"][0]["text"] == "r2"

    # An unwrappable TAIL (empty assistant last) walks back to the newest
    # wrappable block instead of dropping the mark.
    tail_empty = [*messages[:2], ChatMessage(role=Role.ASSISTANT, content="")]
    _ = [ev async for ev in gw.stream_tools(tail_empty, tools=_TOOLS)]
    wire = captured["messages"]
    assert [_has_cache_control(m) for m in wire] == [True, True, False]


async def test_anthropic_two_message_wire_marks_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first call of an answer — ``[system, question]`` (len==2): the
    stable anchor and the moving mark are DISTINCT indices (0 and 1); the
    question is written so the tool turn that follows reads it. No aliasing,
    no double-wrap."""
    captured = _capture_acompletion(monkeypatch)
    gw = LLMGateway(_settings(LLM_MODEL="openrouter/anthropic/claude-opus-4.8"))
    _ = [
        ev
        async for ev in gw.stream_tools(
            [
                ChatMessage(role=Role.SYSTEM, content="sys"),
                ChatMessage(role=Role.USER, content="q"),
            ],
            tools=_TOOLS,
        )
    ]
    wire = captured["messages"]
    assert [_has_cache_control(m) for m in wire] == [True, True]
    assert wire[0]["content"][0]["text"] == "sys"
    assert wire[1]["content"][0]["text"] == "q"
    # Exactly one cache_control part per decorated message.
    assert all(len(m["content"]) == 1 for m in wire)


async def test_openai_family_sends_prompt_cache_key_untouched_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI-family models get ``extra_body.prompt_cache_key`` (routing
    affinity for implicit prefix caching) and NO message decoration."""
    captured = _capture_acompletion(monkeypatch)
    gw = LLMGateway(_settings())  # default: openrouter/openai/gpt-4o-mini

    _ = [ev async for ev in gw.stream_tools(_CONVO, tools=_TOOLS, cache_key="sess-42")]

    assert captured["extra_body"] == {"prompt_cache_key": "sess-42"}
    assert all(not _has_cache_control(m) for m in captured["messages"])

    # Without a cache key there is nothing to steer — no extra_body at all.
    _ = [ev async for ev in gw.stream_tools(_CONVO, tools=_TOOLS)]
    assert "extra_body" not in captured


async def test_unknown_family_and_kill_switch_send_bare_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3: an unknown provider family — and ANY family with the
    ``CHAT_PROMPT_CACHE_ENABLED=false`` kill-switch — sends the exact pre-#411
    wire: plain-string contents, no ``cache_control``, no ``extra_body``."""
    captured = _capture_acompletion(monkeypatch)

    # Unknown family (cache_key given, still nothing sent).
    gw = LLMGateway(_settings(LLM_MODEL="openrouter/mistralai/mistral-large"))
    _ = [ev async for ev in gw.stream_tools(_CONVO, tools=_TOOLS, cache_key="sess-9")]
    assert "extra_body" not in captured
    assert all(isinstance(m.get("content"), str) for m in captured["messages"])

    # Kill-switch, Anthropic family: no breakpoints.
    gw = LLMGateway(
        _settings(
            LLM_MODEL="openrouter/anthropic/claude-opus-4.8",
            CHAT_PROMPT_CACHE_ENABLED="false",
        )
    )
    _ = [ev async for ev in gw.stream_tools(_CONVO, tools=_TOOLS, cache_key="sess-9")]
    assert all(isinstance(m.get("content"), str) for m in captured["messages"])

    # Kill-switch, OpenAI family: no prompt_cache_key either.
    gw = LLMGateway(_settings(CHAT_PROMPT_CACHE_ENABLED="false"))
    _ = [ev async for ev in gw.stream_tools(_CONVO, tools=_TOOLS, cache_key="sess-9")]
    assert "extra_body" not in captured


async def test_anthropic_prefix_breakpoint_is_stable_across_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-2: growing the transcript must not perturb the already-sent prefix.

    Turn N+1 = turn N's messages + [assistant, user]. The serialized wire of
    the shared messages must be byte-identical across the two calls — including
    the decorated message[0] — with ONLY the moving mark repositioned to the
    new last block (its old position reverts to a plain string, which is
    exactly the write/read chain: turn N wrote through its last block, turn
    N+1's identical prefix reads it). Any other difference would invalidate
    the provider's cached prefix every turn."""
    captured = _capture_acompletion(monkeypatch)
    gw = LLMGateway(_settings(LLM_MODEL="openrouter/anthropic/claude-opus-4.8"))

    _ = [ev async for ev in gw.stream_tools(_CONVO, tools=_TOOLS, cache_key="s")]
    wire1 = captured["messages"]

    grown = [
        *_CONVO,
        ChatMessage(role=Role.ASSISTANT, content="second answer"),
        ChatMessage(role=Role.USER, content="third question"),
    ]
    _ = [ev async for ev in gw.stream_tools(grown, tools=_TOOLS, cache_key="s")]
    wire2 = captured["messages"]

    # The stable anchor is identical; turn 1's moving mark (index 3) reverted
    # to the plain shape in turn 2; the new mark sits on the new last block.
    assert wire2[0] == wire1[0]
    assert wire2[3]["content"] == "follow-up"
    assert [_has_cache_control(m) for m in wire2] == [True, False, False, False, False, True]
    # Every shared undecorated message is byte-identical across turns.
    assert wire2[1] == wire1[1]
    assert wire2[2] == wire1[2]


async def test_custom_api_base_gets_no_directives_fail_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0016 §2.4 fail-safe: a route with a custom ``api_base`` gets NO
    cache directives regardless of its model NAME — a tenant's OpenAI-
    compatible endpoint named ``openai/my-claude-reranker`` must receive the
    exact undirected wire, not vendor fields its host may reject. The known
    OpenRouter host keeps directives (a tenant's own OpenRouter key routes
    through the same caching provider)."""
    captured = _capture_acompletion(monkeypatch)
    gw = LLMGateway(_settings())

    # Deceptively-named model on a strict custom host: bare wire, both knobs.
    _ = [
        ev
        async for ev in gw.stream_tools(
            _CONVO,
            tools=_TOOLS,
            model="openai/my-claude-reranker",
            api_base="https://strict.example/v1",
            api_key="tenant-key",
            cache_key="sess-7",
        )
    ]
    assert all(isinstance(m.get("content"), str) for m in captured["messages"])
    assert "extra_body" not in captured
    assert captured["api_base"] == "https://strict.example/v1"

    # A gpt-named model on a custom host: no prompt_cache_key either.
    _ = [
        ev
        async for ev in gw.stream_tools(
            _CONVO,
            tools=_TOOLS,
            model="azure/gpt-4o",
            api_base="https://myazure.example/openai",
            api_key="tenant-key",
            cache_key="sess-7",
        )
    ]
    assert "extra_body" not in captured

    # The fail-safe is NOT conditioned on a cache key: same custom host with
    # cache_key=None must be equally bare (round-2 review, finding 5).
    _ = [
        ev
        async for ev in gw.stream_tools(
            _CONVO,
            tools=_TOOLS,
            model="anthropic/claude-opus-4.8",
            api_base="https://strict.example/v1",
            api_key="tenant-key",
        )
    ]
    assert all(isinstance(m.get("content"), str) for m in captured["messages"])

    # A tenant provider whose base IS OpenRouter: directives stay on — family
    # from the raw id's upstream PREFIX (the namespace OpenRouter routes by).
    _ = [
        ev
        async for ev in gw.stream_tools(
            _CONVO,
            tools=_TOOLS,
            model="anthropic/claude-opus-4.8",
            api_base="https://openrouter.ai/api/v1",
            api_key="tenant-key",
        )
    ]
    wire = captured["messages"]
    assert [_has_cache_control(m) for m in wire] == [True, False, False, True]


async def test_no_base_route_needs_openrouter_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-2 blocker: ``api_base=None`` does NOT mean OpenRouter — the
    config registry accepts any LiteLLM id (``anthropic/claude-…``,
    ``openai/my-claude-reranker``) which routes through a NATIVE adapter the
    directives were never validated against. Without the ``openrouter/``
    prefix a no-base route gets NOTHING, whatever its name contains; family
    is the upstream PREFIX, never a name substring."""
    captured = _capture_acompletion(monkeypatch)
    gw = LLMGateway(_settings())

    # Direct-adapter ids with deceptive/real vendor names: bare wire.
    for model in (
        "openai/my-claude-reranker",  # openai adapter, claude in the NAME
        "anthropic/claude-opus-4.8",  # native anthropic adapter — unvalidated
        "azure/gpt-4o",
        "gpt-4o",
    ):
        _ = [
            ev
            async for ev in gw.stream_tools(
                _CONVO, tools=_TOOLS, model=model, cache_key="sess-3"
            )
        ]
        assert all(
            isinstance(m.get("content"), str) for m in captured["messages"]
        ), model
        assert "extra_body" not in captured, model

    # The openrouter/-prefixed forms of the same upstreams: directives on.
    _ = [
        ev
        async for ev in gw.stream_tools(
            _CONVO, tools=_TOOLS, model="openrouter/anthropic/claude-opus-4.8"
        )
    ]
    assert [_has_cache_control(m) for m in captured["messages"]] == [
        True,
        False,
        False,
        True,
    ]
    _ = [
        ev
        async for ev in gw.stream_tools(
            _CONVO, tools=_TOOLS, model="openrouter/openai/gpt-5.5", cache_key="sess-3"
        )
    ]
    assert captured["extra_body"] == {"prompt_cache_key": "sess-3"}
    # The reranker name under the openrouter/openai/ namespace steers by
    # PREFIX (openai), not by the "claude" substring: prompt_cache_key, no marks.
    _ = [
        ev
        async for ev in gw.stream_tools(
            _CONVO,
            tools=_TOOLS,
            model="openrouter/openai/my-claude-reranker",
            cache_key="sess-3",
        )
    ]
    assert captured["extra_body"] == {"prompt_cache_key": "sess-3"}
    assert all(isinstance(m.get("content"), str) for m in captured["messages"])


async def test_family_re_resolves_per_call_across_failover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#413 failover changes the model mid-answer: capability must re-resolve
    on EVERY gateway call from that call's route — an anthropic primary's
    breakpoints must not leak onto an unknown-family fallback's wire."""
    captured = _capture_acompletion(monkeypatch)
    gw = LLMGateway(_settings())

    _ = [
        ev
        async for ev in gw.stream_tools(
            _CONVO, tools=_TOOLS, model="openrouter/anthropic/claude-opus-4.8"
        )
    ]
    assert any(_has_cache_control(m) for m in captured["messages"])

    _ = [
        ev
        async for ev in gw.stream_tools(
            _CONVO, tools=_TOOLS, model="openrouter/mistralai/mistral-large", cache_key="s"
        )
    ]
    assert all(isinstance(m.get("content"), str) for m in captured["messages"])
    assert "extra_body" not in captured
