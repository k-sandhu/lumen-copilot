"""Untrusted tool payload regressions (ADR-0024 #831; INV-6 and INV-8)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import replace
from typing import Any

import litellm
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import AuditEventRepository
from app.domain.audit import AuditAction
from app.domain.entities import AuditOutcome
from app.domain.llm import ChatMessage, Role, ToolCall, ToolSpec
from app.domain.tools import ERROR_BAD_ARGS, ERROR_TOOL_TIMEOUT, RiskTier, ToolHandlerResult
from app.llm.gateway import LLMGateway, _ToolCallAccumulator
from app.mcp import MCP_ERROR_TIMEOUT
from app.mcp.client import McpSession, _to_tool_result
from app.services.tools.runner import hash_args
from app.services.tools.types import ToolContext, ToolDefinition
from mcp.types import CallToolResult, TextContent
from tests.test_llm_tool_calling import (
    _FakeStream,
    _Part,
    _settings,
    _ToolCallFrag,
)
from tests.test_tool_runner import (
    _all_invocations,
    _context,
    _make_runner,
    _World,
)
from tests.test_tool_runner import (
    world as _world_fixture,
)

world = _world_fixture

ARGUMENT_LIMIT = 20_000
RESULT_LIMIT = 12_000
TRUNCATED = "[MCP result truncated; request a narrower result.]"
STRUCTURED_OMITTED = "[MCP structured result omitted; request a narrower result.]"


def _call(raw: Any) -> ToolCall:
    acc = _ToolCallAccumulator()
    acc.feed(call_id="c1", name="probe", arguments=raw)
    return acc.build()


_INVALID_ARGUMENTS = [
    pytest.param('{"q":"HOSTILE-SECRET"', id="invalid-json"),
    pytest.param('"HOSTILE-SECRET"', id="non-object-string"),
    pytest.param("[]", id="non-object-array"),
    pytest.param("null", id="non-object-null"),
    pytest.param('{"q":NaN}', id="nan"),
    pytest.param('{"q":Infinity}', id="infinity"),
    pytest.param('{"q":-Infinity}', id="negative-infinity"),
    pytest.param('{"q":1e9999}', id="exponent-overflow"),
    pytest.param('{"q":"\\ud800"}', id="escaped-surrogate"),
    pytest.param('{"\\udfff":"x"}', id="surrogate-key"),
    pytest.param('{"q":"\ud800"}', id="raw-surrogate"),
    pytest.param('{"q":[{"x":"\\u0000"}]}', id="nested-nul"),
    pytest.param('{"\\u0000":"x"}', id="nul-key"),
    pytest.param(" " * (ARGUMENT_LIMIT + 1), id="over-length"),
    pytest.param('{"q":' + "[" * 1500 + "0" + "]" * 1500 + "}", id="deep-nesting"),
    pytest.param({"q": "HOSTILE-SECRET"}, id="provider-object-arguments"),
    pytest.param(123, id="provider-number-arguments"),
]


@pytest.mark.parametrize("raw", _INVALID_ARGUMENTS)
async def test_invalid_arguments_never_run_optional_read_only_tool(world: _World, raw: Any) -> None:
    invoked: list[dict[str, Any]] = []

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        invoked.append(args)
        return ToolHandlerResult(content="unfiltered data")

    definition = ToolDefinition(
        name="probe",
        description="An all-optional read-only tool.",
        json_schema={"type": "object", "properties": {"q": {}}},
        handler=handler,
    )
    runner, _, _ = _make_runner(
        world, allowed=frozenset({"probe"}), extra_tools={"probe": definition}
    )
    call = _call(raw)
    result = await runner.run(call=call, context=_context(world))
    assert invoked == [], "broken JSON must never turn into an unfiltered call"
    assert not result.ok and result.error == ERROR_BAD_ARGS
    assert "HOSTILE-SECRET" not in result.content
    assert "JSON object" in result.content
    assert call.argument_error is not None
    assert call.arguments == {}
    assert (
        LLMGateway._to_wire_messages([ChatMessage(Role.ASSISTANT, "", (call,))])[0]["tool_calls"][
            0
        ]["function"]["arguments"]
        == "{}"
    )
    await world.session.commit()
    async with AsyncSession(world.session.bind) as fresh:
        events = await AuditEventRepository(fresh, world.tenant_id).list_recent(limit=10)
        assert {e.action for e in events} == {
            AuditAction.TOOL_INVOKED.value,
            AuditAction.TOOL_RESULT.value,
        }
        assert all(e.outcome == AuditOutcome.DENIED for e in events)
        assert all(e.metadata["error"] == ERROR_BAD_ARGS for e in events)
        assert "HOSTILE-SECRET" not in json.dumps([e.metadata for e in events])


@pytest.mark.parametrize("raw", ["", " \t\n", "{}", '{"q":"\\ud83d\\ude00"}', '{"q":"\\\\u0000"}'])
async def test_valid_arguments_still_run(world: _World, raw: str) -> None:
    seen: list[dict[str, Any]] = []

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        seen.append(args)
        return ToolHandlerResult(content="ok")

    definition = ToolDefinition(
        name="probe",
        description="d",
        json_schema={"type": "object", "properties": {"q": {}}},
        handler=handler,
    )
    runner, _, _ = _make_runner(
        world, allowed=frozenset({"probe"}), extra_tools={"probe": definition}
    )
    call = _call(raw)
    result = await runner.run(call=call, context=_context(world))
    assert result.ok and seen == [json.loads(raw.strip() or "{}")]
    assert getattr(call, "argument_error", None) is None
    assert hash_args(call.arguments)


async def test_decode_refusal_precedes_approval_and_never_echoes_error(world: _World) -> None:
    class Gate:
        async def request(self, request: Any) -> Any:
            raise AssertionError("invalid arguments must not request approval")

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        raise AssertionError("invalid arguments must not run")

    definition = ToolDefinition(
        name="probe",
        description="d",
        json_schema={"type": "object"},
        handler=handler,
        read_only=False,
        risk_tier=RiskTier.T2,
        requires_approval=True,
    )
    runner, _, _ = _make_runner(
        world, allowed=frozenset({"probe"}), extra_tools={"probe": definition}, gate=Gate()
    )
    call = replace(_call(""), argument_error="HOSTILE-SECRET")
    result = await runner.run(call=call, context=_context(world))
    assert not result.ok and result.error == ERROR_BAD_ARGS
    assert "HOSTILE-SECRET" not in result.content


@pytest.mark.parametrize("argument_error", ["HOSTILE-SECRET", ""])
def test_rejected_call_replays_safe_arguments(argument_error: str) -> None:
    call = ToolCall(
        id="c1", name="probe", arguments={"q": "HOSTILE-SECRET"}, argument_error=argument_error
    )
    wire = LLMGateway._to_wire_messages([ChatMessage(Role.ASSISTANT, "", (call,))])
    assert wire[0]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_argument_cap_is_inclusive_and_overflow_cannot_be_rehabilitated() -> None:
    raw = '{"q":"' + "x" * (ARGUMENT_LIMIT - 8) + '"}'
    assert len(raw) == ARGUMENT_LIMIT
    assert getattr(_call(raw), "argument_error", None) is None
    acc = _ToolCallAccumulator()
    acc.feed(call_id="c1", name="probe", arguments=raw)
    acc.feed(call_id=None, name=None, arguments=" ")
    acc.feed(call_id=None, name=None, arguments="{}")
    assert len(acc._args) <= ARGUMENT_LIMIT
    assert acc.build().argument_error is not None


@pytest.mark.parametrize(
    "ids", [(None, None), ("same", "same"), (None, "call_1_0"), (" ", " "), (123, 123)]
)
async def test_call_ids_pair_results_in_next_provider_request(
    world: _World, monkeypatch: pytest.MonkeyPatch, ids: tuple[Any, Any]
) -> None:
    captured: list[dict[str, Any]] = []
    name = "mcp:server:probe"

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        return ToolHandlerResult(content=f"result-{args['q']}")

    definition = ToolDefinition(
        name=name,
        description="d",
        json_schema={"type": "object", "properties": {"q": {"type": "integer"}}},
        handler=handler,
    )

    async def provider(**kwargs: Any) -> _FakeStream:
        captured.append(kwargs)
        wire_name = kwargs["tools"][0]["function"]["name"]
        return _FakeStream(
            [
                _Part(
                    tool_calls=[
                        _ToolCallFrag(
                            index=i, id=call_id, name=wire_name, arguments=json.dumps({"q": i})
                        )
                        for i, call_id in enumerate(ids)
                    ],
                    finish_reason="tool_calls",
                )
            ]
        )

    monkeypatch.setattr(litellm, "acompletion", provider)
    gateway = LLMGateway(_settings())
    messages = [ChatMessage(Role.USER, "q")]
    spec = ToolSpec(name=name, description="d", parameters=definition.json_schema)
    events = [e async for e in gateway.stream_tools(messages, tools=[spec])]
    calls = events[-1].tool_calls
    assert len({c.id for c in calls}) == 2 and all(
        isinstance(c.id, str) and c.id.strip() for c in calls
    )
    assert all(c.name == name for c in calls)
    if ids[1] == "call_1_0":
        assert calls[1].id == ids[1], "a generated id must not steal a later real id"
    runner, audit, _ = _make_runner(
        world, allowed=frozenset({name}), extra_tools={name: definition}
    )
    results = [await runner.run(call=c, context=_context(world)) for c in calls]
    messages.append(ChatMessage(Role.ASSISTANT, "", calls))
    messages.extend(
        ChatMessage(Role.TOOL, r.content, tool_call_id=r.call_id, name=r.name) for r in results
    )
    _ = [e async for e in gateway.stream_tools(messages, tools=[spec])]
    wire = captured[-1]["messages"]
    assert [(c["id"], c["function"]["arguments"]) for c in wire[1]["tool_calls"]] == [
        (c.id, json.dumps(c.arguments)) for c in calls
    ]
    assert [(m["tool_call_id"], m["content"]) for m in wire[2:]] == [
        (c.id, f"result-{i}") for i, c in enumerate(calls)
    ]
    assert {e.metadata["call_id"] for e in await audit.list_recent(limit=10)} == {
        c.id for c in calls
    }
    invocations = await _all_invocations(world)
    assert [(i.ordinal, i.args_hash) for i in sorted(invocations, key=lambda row: row.ordinal)] == [
        (i, hash_args(c.arguments)) for i, c in enumerate(calls)
    ]
    assert not ({c.id for c in calls} & {c.id for c in (await _terminal(gateway, messages, spec))})


async def _terminal(
    gateway: LLMGateway, messages: list[ChatMessage], spec: ToolSpec
) -> tuple[ToolCall, ...]:
    events = [e async for e in gateway.stream_tools(messages, tools=[spec])]
    return events[-1].tool_calls


@pytest.mark.parametrize("is_error", [False, True])
@pytest.mark.parametrize("size", [RESULT_LIMIT, RESULT_LIMIT + 1, RESULT_LIMIT * 50])
def test_mcp_text_result_bound(size: int, is_error: bool) -> None:
    result = _to_tool_result(
        "probe",
        CallToolResult(content=[TextContent(type="text", text="x" * size)], isError=is_error),
    )
    assert len(result.content) <= RESULT_LIMIT
    assert result.ok is not is_error
    if size > RESULT_LIMIT:
        assert result.content.endswith(TRUNCATED)
    else:
        assert result.content == "x" * RESULT_LIMIT


def test_mcp_many_blocks_and_unicode_boundary() -> None:
    prefix = "x" * (RESULT_LIMIT - len(TRUNCATED) - 2)
    result = _to_tool_result(
        "probe",
        CallToolResult(
            content=[
                TextContent(type="text", text=prefix + "\ud83d\ude00"),
                *[TextContent(type="text", text="tail") for _ in range(4000)],
            ]
        ),
    )
    assert len(result.content) <= RESULT_LIMIT
    assert result.content.endswith(TRUNCATED)
    result.content.encode("utf-8")
    assert "\ufffd" not in result.content, "a valid surrogate pair must not be split"


@pytest.mark.parametrize("size", [RESULT_LIMIT - 9, RESULT_LIMIT - 8, RESULT_LIMIT * 50])
def test_mcp_structured_result_shares_text_budget(size: int) -> None:
    structured = {"x": "s" * size}
    serialized = json.dumps(structured, ensure_ascii=False)
    result = _to_tool_result("probe", CallToolResult(content=[], structuredContent=structured))
    if len(serialized) <= RESULT_LIMIT:
        assert result.structured == structured and result.content == ""
    else:
        assert result.structured is None
        assert result.content.endswith(STRUCTURED_OMITTED)
    assert (
        len(result.content)
        + (len(json.dumps(result.structured, ensure_ascii=False)) if result.structured else 0)
        <= RESULT_LIMIT
    )


def test_mcp_structured_overflow_marks_and_preserves_text_bound() -> None:
    result = _to_tool_result(
        "probe",
        CallToolResult(
            content=[TextContent(type="text", text="x" * RESULT_LIMIT)],
            structuredContent={"x": "secret" * RESULT_LIMIT},
        ),
    )
    assert result.structured is None
    assert len(result.content) <= RESULT_LIMIT
    assert result.content.endswith(STRUCTURED_OMITTED)


@pytest.mark.parametrize("mode", ["return", "error", "timeout-error"])
@pytest.mark.parametrize("tool_timeout", [None, 60.0])
async def test_swallowed_cancellation_still_cancels(
    world: _World, mode: str, tool_timeout: float | None
) -> None:
    entered = asyncio.Event()

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if mode == "error":
                raise RuntimeError("HOSTILE-SECRET") from None
            if mode == "timeout-error":
                raise TimeoutError("HOSTILE-SECRET") from None
            return ToolHandlerResult(content="late")
        raise AssertionError("unreachable")

    definition = ToolDefinition(
        name="probe",
        description="d",
        json_schema={"type": "object"},
        handler=handler,
        timeout_seconds=tool_timeout,
    )
    runner, audit, _ = _make_runner(
        world, allowed=frozenset({"probe"}), extra_tools={"probe": definition}
    )
    task = asyncio.create_task(runner.run(call=_call(""), context=_context(world)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events = await audit.list_recent(limit=10)
    assert len(events) == 2
    assert all(e.metadata["error"] == "tool_cancelled" for e in events)
    assert all(e.outcome == AuditOutcome.ERROR for e in events)


async def test_swallowed_timeout_discards_late_result(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fire the scheduled deadline by handshake, without waiting for wall time."""
    loop = asyncio.get_running_loop()
    original_call_at = loop.call_at
    deadlines: list[tuple[asyncio.TimerHandle, Callable[..., Any], tuple[Any, ...]]] = []

    def call_at(
        when: float, callback: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> asyncio.TimerHandle:
        handle = original_call_at(when, callback, *args, **kwargs)
        deadlines.append((handle, callback, args))
        return handle

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        handle, callback, callback_args = deadlines[0]
        handle.cancel()
        loop.call_soon(callback, *callback_args)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return ToolHandlerResult(content="late data must be discarded")
        raise AssertionError("unreachable")

    definition = ToolDefinition(
        name="probe",
        description="d",
        json_schema={"type": "object"},
        handler=handler,
        timeout_seconds=60.0,
    )
    runner, _, _ = _make_runner(
        world, allowed=frozenset({"probe"}), extra_tools={"probe": definition}
    )
    with monkeypatch.context() as patch:
        patch.setattr(loop, "call_at", call_at)
        result = await runner.run(call=_call(""), context=_context(world))
    assert not result.ok and result.error == ERROR_TOOL_TIMEOUT
    assert "late data" not in result.content


async def test_mcp_adapter_does_not_convert_external_cancellation_to_result() -> None:
    entered = asyncio.Event()

    class Session:
        async def call_tool(self, name: str, *, arguments: dict[str, Any]) -> CallToolResult:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    session = McpSession(
        slug="probe", session=Session(), exit_stack=AsyncExitStack(), call_timeout_seconds=60.0
    )  # type: ignore[arg-type]
    task = asyncio.create_task(session.call_tool("probe", {}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("seconds", [0.0, -1.0])
async def test_nonpositive_tool_timeout_never_dispatches(world: _World, seconds: float) -> None:
    invoked: list[str] = []

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        invoked.append("ran")
        return ToolHandlerResult(content="already too late")

    definition = ToolDefinition(
        name="probe",
        description="d",
        json_schema={"type": "object"},
        handler=handler,
        timeout_seconds=seconds,
    )
    runner, _, _ = _make_runner(
        world, allowed=frozenset({"probe"}), extra_tools={"probe": definition}
    )
    result = await runner.run(call=_call(""), context=_context(world))
    assert invoked == []
    assert not result.ok and result.error == ERROR_TOOL_TIMEOUT


@pytest.mark.parametrize("seconds", [0.0, -1.0])
async def test_mcp_nonpositive_timeout_never_dispatches(seconds: float) -> None:
    invoked: list[str] = []

    class Session:
        async def call_tool(self, name: str, *, arguments: dict[str, Any]) -> CallToolResult:
            invoked.append("ran")
            return CallToolResult(content=[TextContent(type="text", text="already too late")])

    session = McpSession(
        slug="probe",
        session=Session(),
        exit_stack=AsyncExitStack(),
        call_timeout_seconds=seconds,
    )  # type: ignore[arg-type]
    result = await session.call_tool("probe", {})
    assert invoked == []
    assert not result.ok and result.error_code == MCP_ERROR_TIMEOUT
