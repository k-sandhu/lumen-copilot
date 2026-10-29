"""Observability tests — OpenTelemetry spans + metrics for Arize Phoenix (#817, ADR-0027).

Telemetry is configured against an in-memory span exporter and metric reader
(the module's test seams), so nothing leaves the process. Covers:

* off by default: no spans, no metrics, no exporter (AC-6);
* the turn tree — AGENT root with TOOL / RETRIEVER / LLM children (AC-1);
* gateway LLM spans: messages, tools, output tool calls, token counts, TTFT,
  and exactly-once ending on success, provider error and early close (AC-3);
* content capture off drops content but keeps structure (AC-4);
* the provider key never reaches a span (AC-5);
* an exporter that fails never fails the caller (AC-6);
* HTTP metrics label by route template, never by raw path;
* the settings guard on unauthenticated content export outside ``local``;
* the ADR-0004 boundary: only ``app/observability`` imports OpenTelemetry.
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from collections.abc import Iterator, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import Any

import litellm
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.core.config import Settings
from app.core.errors import AppError
from app.domain.llm import ChatMessage, Role, StreamEvent, TokenUsage, ToolCall, ToolSpec
from app.llm.gateway import LLMGateway
from app.observability import (
    HttpMetricsMiddleware,
    LlmCall,
    configure_telemetry,
    observe_tool,
    observe_turn,
    shutdown_telemetry,
)
from app.observability.telemetry import tracer
from app.realtime.backplane import InMemoryBackplane
from tests import test_chat_runtime
from tests.test_chat_runtime import (
    _Ctx,
    _drain,
    _FakeRetrieval,
    _passage,
    _runtime,
    _ScriptedGateway,
)

# The chat-runtime suite's seeded tenant/user/session fixture, reused as-is.
ctx = test_chat_runtime.ctx

_API_KEY = "sk-test-observability-key-7f3a"

_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:5432/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "CELERY_BROKER_URL": "redis://localhost:6379/1",
    "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_ACCESS_KEY": "test",
    "S3_SECRET_KEY": "test_secret",
    "S3_BUCKET": "test-bucket",
    "OPENROUTER_API_KEY": _API_KEY,
    "LLM_MODEL": "openrouter/openai/gpt-4o-mini",
    "LLM_TIMEOUT_SECONDS": "60",
}


def _settings(**overrides: str) -> Settings:
    return Settings(**{**_BASE_ENV, **overrides})  # type: ignore[arg-type]


class _Telemetry:
    def __init__(self, spans: InMemorySpanExporter, metrics: InMemoryMetricReader) -> None:
        self.exporter = spans
        self.reader = metrics

    @property
    def spans(self) -> list[ReadableSpan]:
        return list(self.exporter.get_finished_spans())

    def named(self, name: str) -> list[ReadableSpan]:
        return [span for span in self.spans if span.name == name]

    def metric_points(self, name: str) -> list[Any]:
        data = self.reader.get_metrics_data()
        points: list[Any] = []
        if data is None:
            return points
        for resource_metrics in data.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    if metric.name == name:
                        points.extend(metric.data.data_points)
        return points


def _enabled(**overrides: str) -> Settings:
    # The suite runs as ENVIRONMENT=test, which the ADR-0027 §4 guard treats as
    # deployed: content export needs a trace key (unused by the injected exporter).
    return _settings(
        TELEMETRY_ENABLED="true", TELEMETRY_TRACES_API_KEY="phoenix-test-key", **overrides
    )


def _configure(**overrides: str) -> _Telemetry:
    exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
    assert configure_telemetry(
        _enabled(**overrides),
        process="test",
        span_exporter=exporter,
        metric_reader=reader,
    )
    return _Telemetry(exporter, reader)


@pytest.fixture
def telemetry() -> Iterator[_Telemetry]:
    shutdown_telemetry()
    try:
        yield _configure()
    finally:
        shutdown_telemetry()


@pytest.fixture
def telemetry_without_content() -> Iterator[_Telemetry]:
    shutdown_telemetry()
    try:
        yield _configure(TELEMETRY_CAPTURE_CONTENT="false")
    finally:
        shutdown_telemetry()


def _all_attribute_text(spans: Sequence[ReadableSpan]) -> str:
    parts: list[str] = []
    for span in spans:
        parts.extend(f"{key}={value}" for key, value in (span.attributes or {}).items())
        for event in span.events:
            parts.extend(str(value) for value in (event.attributes or {}).values())
        parts.append(str(span.status.description))
    return "\n".join(parts)


# --- Off by default -----------------------------------------------------------


def test_telemetry_is_off_by_default_and_helpers_are_no_ops() -> None:
    shutdown_telemetry()
    settings = _settings()
    assert settings.telemetry_enabled is False
    assert configure_telemetry(settings, process="test") is False

    with observe_turn(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        model="m",
        question="q",
        interactive=True,
    ) as turn:
        turn.answered(
            answer="a", model_used="m", finish_reason="stop", citation_count=0, total_tokens=1
        )
    assert not tracer().start_span("probe").is_recording()


def test_configure_is_idempotent_and_shutdown_returns_to_no_op(telemetry: _Telemetry) -> None:
    assert configure_telemetry(_enabled(), process="test") is True
    with observe_tool(name="search_text", call_id="c1", arguments={}, ordinal=0):
        pass
    assert len(telemetry.named("tool.search_text")) == 1
    resource = telemetry.spans[0].resource.attributes
    assert resource["service.name"] == "lumen-copilot-backend"
    assert resource["openinference.project.name"] == "lumen-copilot"
    assert resource["lumen.process"] == "test"

    shutdown_telemetry()
    assert not tracer().start_span("probe").is_recording()


# --- Gateway LLM spans ----------------------------------------------------------


class _Fn:
    def __init__(self, name: str | None, arguments: str | None) -> None:
        self.name = name
        self.arguments = arguments


class _Frag:
    def __init__(
        self, *, index: int, id: str | None, name: str | None, arguments: str | None
    ) -> None:
        self.index = index
        self.id = id
        self.function = _Fn(name, arguments)


class _Delta:
    def __init__(self, content: str | None = None, tool_calls: list[_Frag] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta: _Delta, finish_reason: str | None = None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason


class _Usage:
    prompt_tokens = 120
    completion_tokens = 30
    total_tokens = 150
    prompt_tokens_details = {"cached_tokens": 100}


class _Chunk:
    def __init__(self, choices: list[_Choice], usage: object = None) -> None:
        self.choices = choices
        self.usage = usage


class _Stream:
    def __init__(self, chunks: list[_Chunk], *, fail_after: int | None = None) -> None:
        self._chunks = chunks
        self._fail_after = fail_after
        self.closed = False

    def __aiter__(self) -> _Stream:
        self._index = 0
        return self

    async def __anext__(self) -> _Chunk:
        if self._fail_after is not None and self._index == self._fail_after:
            raise litellm.exceptions.APIConnectionError(
                message=f"upstream reset key={_API_KEY}", llm_provider="openrouter", model="m"
            )
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    async def aclose(self) -> None:
        self.closed = True


def _tool_turn_chunks() -> list[_Chunk]:
    return [
        _Chunk([_Choice(_Delta(content="Let me check. "))]),
        _Chunk(
            [
                _Choice(
                    _Delta(
                        tool_calls=[
                            _Frag(index=0, id="call_1", name="search_text", arguments='{"query": ')
                        ]
                    )
                )
            ]
        ),
        _Chunk(
            [
                _Choice(
                    _Delta(
                        tool_calls=[_Frag(index=0, id=None, name=None, arguments='"deduction"}')]
                    )
                )
            ]
        ),
        _Chunk([_Choice(_Delta(), finish_reason="tool_calls")], usage=_Usage()),
    ]


_MESSAGES = [
    ChatMessage(role=Role.SYSTEM, content="You are grounded."),
    ChatMessage(role=Role.USER, content="What is the standard deduction?"),
]
_TOOLS = (
    ToolSpec(
        name="search_text",
        description="search the corpus",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    ),
)


def _patch_acompletion(monkeypatch: pytest.MonkeyPatch, stream: object) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def _fake(**kwargs: Any) -> object:
        seen.append(kwargs)
        return stream

    monkeypatch.setattr(litellm, "acompletion", _fake)
    return seen


async def test_stream_tools_records_an_openinference_llm_span(
    telemetry: _Telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _patch_acompletion(monkeypatch, _Stream(_tool_turn_chunks()))
    gateway = LLMGateway(_settings())
    async with aclosing(gateway.stream_tools(_MESSAGES, tools=_TOOLS, max_tokens=512)) as events:
        collected = [event async for event in events]
    assert collected[-1].tool_calls[0].arguments == {"query": "deduction"}
    assert seen[0]["api_key"] == _API_KEY  # the provider still gets its key...

    [span] = telemetry.named("llm.stream_tools")
    attrs = span.attributes or {}
    assert attrs["openinference.span.kind"] == "LLM"
    assert attrs["llm.model_name"] == "openrouter/openai/gpt-4o-mini"
    assert attrs["llm.provider"] == "openrouter"
    assert attrs["llm.input_messages.0.message.role"] == "system"
    assert attrs["llm.input_messages.1.message.content"] == "What is the standard deduction?"
    assert "search_text" in str(attrs["llm.tools.0.tool.json_schema"])
    assert attrs["llm.output_messages.0.message.content"] == "Let me check. "
    assert attrs["llm.output_messages.0.message.tool_calls.0.tool_call.function.name"] == (
        "search_text"
    )
    assert attrs["llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments"] == (
        '{"query": "deduction"}'
    )
    assert attrs["llm.token_count.prompt"] == 120
    assert attrs["llm.token_count.completion"] == 30
    assert attrs["llm.token_count.prompt_details.cache_read"] == 100
    assert '"max_tokens": 512' in str(attrs["llm.invocation_parameters"])
    assert "lumen.llm.time_to_first_token_ms" in attrs
    assert span.status.status_code is StatusCode.OK

    # ...but no span attribute ever carries it (AC-5).
    assert _API_KEY not in _all_attribute_text(telemetry.spans)
    assert "api_key" not in str(attrs["llm.invocation_parameters"])

    tokens = {
        point.attributes["gen_ai.token.type"]: point.value
        for point in telemetry.metric_points("lumen.llm.tokens")
    }
    assert tokens == {"input": 120, "output": 30, "cache_read": 100}
    [duration] = telemetry.metric_points("gen_ai.client.operation.duration")
    assert duration.count == 1 and "error.type" not in duration.attributes
    assert len(telemetry.metric_points("lumen.llm.time_to_first_token")) == 1


async def test_stream_tools_span_ends_once_on_provider_error(
    telemetry: _Telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_acompletion(monkeypatch, _Stream(_tool_turn_chunks(), fail_after=1))
    gateway = LLMGateway(_settings())
    with pytest.raises(AppError):
        async with aclosing(gateway.stream_tools(_MESSAGES, tools=_TOOLS)) as events:
            async for _ in events:
                pass

    [span] = telemetry.named("llm.stream_tools")
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes and span.attributes["error.type"]
    # The vendor message (which here embeds the key) is never recorded.
    assert _API_KEY not in _all_attribute_text(telemetry.spans)
    [duration] = telemetry.metric_points("gen_ai.client.operation.duration")
    assert duration.attributes["error.type"] == span.attributes["error.type"]


async def test_stream_tools_span_ends_once_when_the_consumer_closes_early(
    telemetry: _Telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _Stream(_tool_turn_chunks())
    _patch_acompletion(monkeypatch, stream)
    gateway = LLMGateway(_settings())
    async with aclosing(gateway.stream_tools(_MESSAGES, tools=_TOOLS)) as events:
        async for event in events:
            if event.text:
                break

    assert stream.closed
    [span] = telemetry.named("llm.stream_tools")
    assert (span.attributes or {})["lumen.llm.stream_closed_early"] is True
    assert span.status.status_code is not StatusCode.ERROR


async def test_stream_tools_span_records_cancellation(
    telemetry: _Telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()

    class _Hanging(_Stream):
        async def __anext__(self) -> _Chunk:
            await gate.wait()
            raise StopAsyncIteration

    _patch_acompletion(monkeypatch, _Hanging([]))
    gateway = LLMGateway(_settings())

    async def _consume() -> None:
        async with aclosing(gateway.stream_tools(_MESSAGES, tools=_TOOLS)) as events:
            async for _ in events:
                pass

    task = asyncio.create_task(_consume())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [span] = telemetry.named("llm.stream_tools")
    assert span.status.status_code is StatusCode.ERROR
    assert (span.attributes or {})["error.type"] == "cancelled"


async def test_chat_records_an_llm_span(
    telemetry: _Telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Message:
        content = "Fourteen thousand six hundred."

    class _Response:
        choices = [type("C", (), {"message": _Message(), "finish_reason": "stop"})()]
        usage = _Usage()

    _patch_acompletion(monkeypatch, _Response())
    completion = await LLMGateway(_settings()).chat(_MESSAGES, max_tokens=64)
    assert completion.content == "Fourteen thousand six hundred."

    [span] = telemetry.named("llm.chat")
    attrs = span.attributes or {}
    assert attrs["llm.output_messages.0.message.content"] == "Fourteen thousand six hundred."
    assert attrs["llm.token_count.total"] == 150
    assert attrs["output.value"] == "Fourteen thousand six hundred."
    assert _API_KEY not in _all_attribute_text(telemetry.spans)


async def test_content_capture_off_keeps_structure_and_drops_content(
    telemetry_without_content: _Telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_acompletion(monkeypatch, _Stream(_tool_turn_chunks()))
    gateway = LLMGateway(_settings())
    async with aclosing(gateway.stream_tools(_MESSAGES, tools=_TOOLS)) as events:
        async for _ in events:
            pass
    with observe_tool(
        name="search_text", call_id="c1", arguments={"query": "secret plan"}, ordinal=0
    ):
        pass

    text = _all_attribute_text(telemetry_without_content.spans)
    assert "standard deduction" not in text
    assert "Let me check" not in text
    assert "deduction" not in text
    assert "secret plan" not in text
    [span] = telemetry_without_content.named("llm.stream_tools")
    attrs = span.attributes or {}
    assert attrs["llm.input_messages.1.message.role"] == "user"
    assert attrs["llm.output_messages.0.message.tool_calls.0.tool_call.function.name"] == (
        "search_text"
    )
    assert attrs["llm.token_count.prompt"] == 120
    assert "input.value" not in attrs and "output.value" not in attrs


# --- Turn tree through the chat runtime -----------------------------------------


async def test_chat_turn_is_one_trace_with_tool_children(telemetry: _Telemetry, ctx: _Ctx) -> None:
    passage = _passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(
                        ToolCall(id="c1", name="search_text", arguments={"query": "deduction"}),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [
                StreamEvent(text="The 2024 standard deduction is $14,600 [S1]."),
                StreamEvent(
                    finish_reason="stop",
                    usage=TokenUsage(prompt_tokens=9, completion_tokens=3, total_tokens=12),
                ),
            ],
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(
        ctx, gateway=gateway, retrieval=_FakeRetrieval([passage]), backplane=backplane
    )
    ok = await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="What is the 2024 standard deduction?",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    await asyncio.wait_for(consumer, timeout=2.0)
    assert ok

    [turn] = telemetry.named("chat.turn")
    attrs = turn.attributes or {}
    assert turn.parent is None
    assert attrs["openinference.span.kind"] == "AGENT"
    assert attrs["session.id"] == str(ctx.session_id)
    assert attrs["user.id"] == str(ctx.principal.user_id)
    assert attrs["input.value"] == "What is the 2024 standard deduction?"
    assert "14,600" in str(attrs["output.value"])
    assert attrs["lumen.chat.citation_count"] == 1
    assert turn.status.status_code is StatusCode.OK

    [tool] = telemetry.named("tool.search_text")
    assert tool.context.trace_id == turn.context.trace_id
    assert tool.parent is not None and tool.parent.span_id == turn.context.span_id
    assert (tool.attributes or {})["openinference.span.kind"] == "TOOL"
    assert '"deduction"' in str((tool.attributes or {})["input.value"])

    [turns] = telemetry.metric_points("lumen.chat.turns")
    assert turns.attributes == {"outcome": "ok", "mode": "interactive"} and turns.value == 1
    [tool_calls] = telemetry.metric_points("lumen.tool.calls")
    assert tool_calls.attributes == {"lumen.tool.name": "search_text", "outcome": "ok"}


async def test_failed_turn_marks_the_root_span_failed(telemetry: _Telemetry, ctx: _Ctx) -> None:
    from tests.test_chat_runtime import _BoomGateway

    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(
        ctx, gateway=_BoomGateway(), retrieval=_FakeRetrieval([]), backplane=backplane
    )
    ok = await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    await asyncio.wait_for(consumer, timeout=2.0)
    assert not ok

    [turn] = telemetry.named("chat.turn")
    assert turn.status.status_code is StatusCode.ERROR
    [turns] = telemetry.metric_points("lumen.chat.turns")
    assert turns.attributes["outcome"] == "error"


async def test_denied_tool_span_is_failed(telemetry: _Telemetry) -> None:
    from app.domain.tools import ToolResult

    with observe_tool(name="write_file", call_id="c9", arguments={}, ordinal=3) as span:
        span.finished(
            ToolResult.failure(
                call_id="c9",
                name="write_file",
                error="not_permitted",
                content="no",
                summary="not permitted",
            )
        )
    [tool] = telemetry.named("tool.write_file")
    assert tool.status.status_code is StatusCode.ERROR
    assert (tool.attributes or {})["lumen.tool.error"] == "not_permitted"


# --- Export failures never reach the caller -------------------------------------


def test_a_failing_exporter_never_fails_the_caller() -> None:
    class _Down(InMemorySpanExporter):
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            raise ConnectionError("collector unreachable")

    shutdown_telemetry()
    try:
        configure_telemetry(
            _enabled(),
            process="test",
            span_exporter=_Down(),
            metric_reader=InMemoryMetricReader(),
        )
        call = LlmCall(method="chat", model="m", messages=_MESSAGES)
        call.succeed(
            content="ok", usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        )
    finally:
        shutdown_telemetry()


# --- HTTP metrics -----------------------------------------------------------------


def test_http_metrics_use_the_route_template(telemetry: _Telemetry) -> None:
    app = FastAPI()
    app.add_middleware(HttpMetricsMiddleware)

    @app.get("/items/{item_id}")
    async def _item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    with TestClient(app) as client:
        assert client.get("/items/abc").status_code == 200
        assert client.get("/items/def").status_code == 200
        assert client.get("/nope/123").status_code == 404

    by_route = {
        (point.attributes["http.route"], point.attributes["http.response.status_code"]): point.count
        for point in telemetry.metric_points("http.server.request.duration")
    }
    assert by_route == {("/items/{item_id}", 200): 2, ("unmatched", 404): 1}


# --- Settings guard -----------------------------------------------------------------


def test_content_export_outside_local_requires_a_trace_key() -> None:
    with pytest.raises(ValueError, match="TELEMETRY_TRACES_API_KEY"):
        _settings(
            ENVIRONMENT="production",
            JWT_SECRET="a-real-secret",
            TELEMETRY_ENABLED="true",
        )
    keyed = _settings(
        ENVIRONMENT="production",
        JWT_SECRET="a-real-secret",
        TELEMETRY_ENABLED="true",
        TELEMETRY_TRACES_API_KEY="phoenix-key",
    )
    assert keyed.telemetry_traces_api_key == "phoenix-key"
    local = _settings(ENVIRONMENT="local", TELEMETRY_ENABLED="true")
    assert local.telemetry_traces_api_key == ""
    no_content = _settings(TELEMETRY_ENABLED="true", TELEMETRY_CAPTURE_CONTENT="false")
    assert no_content.telemetry_capture_content is False


# --- ADR-0004 boundary --------------------------------------------------------------


def test_only_the_observability_module_imports_opentelemetry() -> None:
    app_root = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for path in app_root.rglob("*.py"):
        if path.is_relative_to(app_root / "observability"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n.split(".")[0] in {"opentelemetry", "openinference"} for n in names):
                offenders.append(str(path.relative_to(app_root)))
    assert offenders == []
