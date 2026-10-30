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
import weakref
from collections import OrderedDict
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
    observe_headless_run,
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
    with observe_tool(
        name="search_text", resolved_name="search_text", call_id="c1", arguments={}, ordinal=0
    ):
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


@pytest.mark.parametrize("stage", ["choices", "usage", "succeed"])
async def test_malformed_chat_ends_span_and_reraises_the_same_exception(
    telemetry: _Telemetry, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    from types import SimpleNamespace

    from app.llm import gateway as gateway_module

    failure = IndexError(f"malformed 200 {_API_KEY}")

    class _BrokenChoices:
        def __getitem__(self, index: int) -> None:
            raise failure

    def _fail(*args: Any, **kwargs: Any) -> None:
        raise failure

    choices = [SimpleNamespace(message=SimpleNamespace(content="answer"), finish_reason="stop")]
    response = SimpleNamespace(choices=_BrokenChoices() if stage == "choices" else choices)
    _patch_acompletion(monkeypatch, response)
    if stage == "usage":
        monkeypatch.setattr(gateway_module, "_extract_usage", _fail)
    if stage == "succeed":
        monkeypatch.setattr(LlmCall, "succeed", _fail)
    with pytest.raises(IndexError) as caught:
        await LLMGateway(_settings()).chat(_MESSAGES)
    assert caught.value is failure
    [span] = telemetry.named("llm.chat")
    assert span.status.status_code is StatusCode.ERROR
    assert (span.attributes or {})["error.type"] == "IndexError"
    assert _API_KEY not in _all_attribute_text(telemetry.spans)
    [duration] = telemetry.metric_points("gen_ai.client.operation.duration")
    assert duration.count == 1 and duration.attributes["error.type"] == "IndexError"


@pytest.mark.parametrize("mode", ["off", "structure", "content"])
@pytest.mark.parametrize("method", ["stream", "stream_tools"])
async def test_stream_retains_text_only_when_recording_content(
    monkeypatch: pytest.MonkeyPatch, mode: str, method: str
) -> None:
    class _Text(str):
        pass

    references: list[weakref.ReferenceType[_Text]] = []

    class _EphemeralStream(_Stream):
        async def __anext__(self) -> _Chunk:
            if self._index == 3:
                self._index += 1
                return _Chunk([_Choice(_Delta(), finish_reason="stop")], usage=_Usage())
            if self._index > 3:
                raise StopAsyncIteration
            text = _Text(f"chunk {self._index} ")
            references.append(weakref.ref(text))
            self._index += 1
            return _Chunk([_Choice(_Delta(content=text))])

    shutdown_telemetry()
    try:
        observed = (
            None if mode == "off" else _configure(TELEMETRY_CAPTURE_CONTENT=str(mode == "content"))
        )
        stream = _EphemeralStream([])
        _patch_acompletion(monkeypatch, stream)
        gateway = LLMGateway(_settings())
        events = (
            gateway.stream(_MESSAGES)
            if method == "stream"
            else gateway.stream_tools(_MESSAGES, tools=_TOOLS)
        )
        text_count = 0
        async with aclosing(events):
            async for event in events:
                text = event.delta if method == "stream" else event.text
                if text:
                    text_count += 1
                if text_count == 3:
                    assert (references[0]() is not None) is (mode == "content")
        assert text_count == 3 and stream.closed
        if observed is not None:
            [span] = observed.named(f"llm.{method}")
            attrs = span.attributes or {}
            assert attrs["llm.output_messages.0.message.role"] == "assistant"
            if mode == "content":
                assert attrs["output.value"] == "chunk 0 chunk 1 chunk 2 "
            else:
                assert "output.value" not in attrs
                assert "llm.output_messages.0.message.content" not in attrs
            if method == "stream_tools":
                assert attrs["llm.finish_reason"] == "stop"
                assert attrs["llm.token_count.total"] == 150
    finally:
        shutdown_telemetry()


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


def _turn(ctx: _Ctx) -> Any:
    return observe_turn(
        session_id=ctx.session_id,
        user_id=ctx.principal.user_id,
        tenant_id=ctx.tenant_id,
        model="m",
        question="deduction",
        interactive=True,
    )


def _assert_child(child: ReadableSpan, parent: ReadableSpan) -> None:
    assert child.context.trace_id == parent.context.trace_id
    assert child.parent is not None and child.parent.span_id == parent.context.span_id


def _patch_embeddings(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from types import SimpleNamespace

    from app.llm import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "_embed_cache", OrderedDict())
    seen: list[dict[str, Any]] = []

    async def _embed(**kwargs: Any) -> object:
        seen.append(kwargs)
        return SimpleNamespace(data=[{"index": 0, "embedding": [0.6, 0.8]}], usage=_Usage())

    monkeypatch.setattr(litellm, "aembedding", _embed)
    return seen


def _embedding_gateway() -> LLMGateway:
    return LLMGateway(
        _settings(
            LLM_EMBEDDING_MODEL="cohere/embed-english-v3.0",
            LLM_EMBEDDING_DIMENSIONS="2",
            LLM_EMBEDDING_API_BASE="",
        )
    )


async def test_retrieval_span_is_parented_under_turn_with_real_search(
    telemetry: _Telemetry, ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.repositories import ChunkInput, ChunkRepository, DocumentRepository
    from app.domain.entities import DocumentStatus
    from app.retrieval.service import RetrievalService
    from app.search import SearchHit
    from tests.test_retrieval_service import _FakeSearchStore

    _patch_embeddings(monkeypatch)
    fingerprint = "b" * 64
    async with ctx.sessionmaker() as session:
        await DocumentRepository(session, ctx.tenant_id).set_status(
            ctx.document_id, DocumentStatus.READY
        )
        [chunk] = await ChunkRepository(session, ctx.tenant_id).replace_for_document(
            ctx.document_id,
            [
                ChunkInput(
                    text="permitted passage",
                    char_start=0,
                    char_end=17,
                    embedding_fingerprint=fingerprint,
                )
            ],
        )
        store = _FakeSearchStore(
            hits=[
                SearchHit(
                    chunk_id=chunk.id,
                    document_id=ctx.document_id,
                    score=0.91,
                    ingestion_attempt=0,
                    embedding_fingerprint=fingerprint,
                )
            ]
        )
        service = RetrievalService(session, gateway=_embedding_gateway(), store=store)  # type: ignore[arg-type]
        with _turn(ctx):
            passages = await service.search(
                principal=ctx.principal, query="deduction", k=7, document_ids=[ctx.document_id]
            )
        assert len(passages) == 1 and passages[0].chunk_id == chunk.id

    [turn] = telemetry.named("chat.turn")
    [retrieval] = telemetry.named("retrieval.search")
    [embedding] = telemetry.named("embedding")
    _assert_child(retrieval, turn)
    _assert_child(embedding, retrieval)
    attrs = retrieval.attributes or {}
    assert attrs["openinference.span.kind"] == "RETRIEVER"
    assert attrs["input.value"] == "deduction"
    assert attrs["lumen.retrieval.k"] == 7
    assert attrs["lumen.retrieval.collection_filter"] == 0
    assert attrs["lumen.retrieval.document_filter"] == 1
    assert attrs["lumen.retrieval.engine_hits"] == attrs["lumen.retrieval.passages"] == 1
    assert attrs["retrieval.documents.0.document.id"] == str(chunk.id)
    assert attrs["retrieval.documents.0.document.score"] == 0.91
    assert attrs["retrieval.documents.0.document.content"] == "permitted passage"
    assert "taxes.pdf" in str(attrs["retrieval.documents.0.document.metadata"])
    assert retrieval.status.status_code is StatusCode.OK


async def test_real_embedding_span_records_cache_hits(
    telemetry: _Telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _patch_embeddings(monkeypatch)
    gateway = _embedding_gateway()
    first = await gateway.embed(["deduction"], cache_namespace="observability-test")
    second = await gateway.embed(["deduction"], cache_namespace="observability-test")
    assert first == second and len(seen) == 1
    spans = telemetry.named("embedding")
    assert len(spans) == 2
    for index, span in enumerate(spans):
        attrs = span.attributes or {}
        assert attrs["openinference.span.kind"] == "EMBEDDING"
        assert attrs["embedding.model_name"] == "cohere/embed-english-v3.0"
        assert attrs["lumen.embedding.input_count"] == 1
        assert attrs["embedding.embeddings.0.embedding.text"] == "deduction"
        assert attrs.get("lumen.embedding.cache_hit", False) is bool(index)
        assert not any("vector" in key for key in attrs)
    assert (spans[0].attributes or {})["llm.token_count.prompt"] == 120


async def test_real_stream_span_is_parented_under_turn(
    telemetry: _Telemetry, ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _Stream([_Chunk([_Choice(_Delta(content="answer"))])])
    _patch_acompletion(monkeypatch, stream)
    with _turn(ctx) as turn:
        chunks = [chunk async for chunk in LLMGateway(_settings()).stream(_MESSAGES)]
        turn.answered(
            answer="answer", model_used="m", finish_reason="stop", citation_count=0, total_tokens=0
        )
    assert [chunk.delta for chunk in chunks] == ["answer"] and stream.closed
    [root] = telemetry.named("chat.turn")
    [llm] = telemetry.named("llm.stream")
    _assert_child(llm, root)
    attrs = llm.attributes or {}
    assert attrs["openinference.span.kind"] == "LLM"
    assert attrs["llm.model_name"] == "openrouter/openai/gpt-4o-mini"
    assert attrs["llm.output_messages.0.message.content"] == attrs["output.value"] == "answer"
    assert llm.status.status_code is StatusCode.OK


async def test_headless_chain_parents_a_real_runtime_turn(telemetry: _Telemetry, ctx: _Ctx) -> None:
    from app.services.run_sink import RunTranscriptSink

    run_id = uuid.uuid4()
    sink = RunTranscriptSink(stream_id=str(run_id))
    runtime = _runtime(
        ctx,
        gateway=_ScriptedGateway([[StreamEvent(text="answer"), StreamEvent(finish_reason="stop")]]),
        retrieval=_FakeRetrieval([]),
        backplane=sink,
        interactive=False,  # type: ignore[arg-type]
    )
    with observe_headless_run(run_id=run_id, assistant_id=None, trigger="manual"):
        assert await runtime.run(
            stream_id=str(run_id),
            session_id=ctx.session_id,
            question="q",
            model="m",
            history=[],
            collection_ids=None,
        )
    [run] = telemetry.named("headless_run")
    [turn] = telemetry.named("chat.turn")
    assert run.parent is None
    assert (run.attributes or {})["openinference.span.kind"] == "CHAIN"
    assert (run.attributes or {})["lumen.run.id"] == str(run_id)
    assert (turn.attributes or {})["openinference.span.kind"] == "AGENT"
    _assert_child(turn, run)


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

    with observe_tool(
        name="write_file", resolved_name="write_file", call_id="c9", arguments={}, ordinal=3
    ) as span:
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


@pytest.mark.parametrize("source", ["unknown", "static", "extra", "off_allow_list"])
async def test_tool_names_are_bounded_by_resolved_definitions(
    telemetry: _Telemetry, ctx: _Ctx, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    from app.services.tools import runner as runner_module
    from tests.test_tool_runner import _context, _make_runner, _ok_tool, _World

    raw_names = ["hallucinated_one", "hallucinated_two"] if source == "unknown" else ["probe"]
    definition = _ok_tool("mcp:server:probe" if source == "extra" else "probe")
    monkeypatch.setattr(
        runner_module,
        "get_tool",
        lambda name: definition if source in {"static", "off_allow_list"} else None,
    )
    expected_name = "unknown" if source == "unknown" else definition.name
    async with ctx.sessionmaker() as session:
        world = _World(session=session, tenant_id=ctx.tenant_id, user_id=ctx.principal.user_id)
        runner, _, _ = _make_runner(
            world,
            allowed=frozenset() if source == "off_allow_list" else frozenset(raw_names),
            extra_tools={raw_names[0]: definition} if source == "extra" else None,
        )
        for index, name in enumerate(raw_names):
            result = await runner.run(
                call=ToolCall(id=f"c{index}", name=name, arguments={}), context=_context(world)
            )
            assert result.ok is (source in {"static", "extra"})

    assert [span.name for span in telemetry.spans] == [f"tool.{expected_name}"] * len(raw_names)
    assert [(span.attributes or {})["tool.name"] for span in telemetry.spans] == raw_names
    for instrument in ("lumen.tool.calls", "lumen.tool.duration"):
        points = telemetry.metric_points(instrument)
        assert {point.attributes["lumen.tool.name"] for point in points} == {expected_name}
        assert sum(getattr(point, "count", getattr(point, "value", 0)) for point in points) == len(
            raw_names
        )


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
