"""Provider-billed accounting regressions for #832 / ADR-0031."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import litellm
import pytest
from jsonschema import Draft202012Validator

from app.db.repositories import LlmUsageRepository
from app.domain.llm import Completion, StreamEvent, TokenUsage, ToolCall
from app.llm.gateway import LLMGateway, _extract_usage
from app.realtime.backplane import InMemoryBackplane
from app.services.chat_runtime import _answer_usage_totals, _RouteState, _Usage
from app.services.provider_models import ModelRoute
from tests.test_chat_runtime import (
    _PRIMARY,
    _Ctx,
    _FakeRetrieval,
    _retryable,
    _runtime,
    _ScriptedGateway,
    _set_fallbacks,
)
from tests.test_chat_runtime import (
    ctx as _runtime_ctx,
)
from tests.test_llm_tool_calling import _settings
from tests.test_session_summary import ctx as _summary_ctx

cost_ctx = _runtime_ctx
summary_ctx = _summary_ctx


@pytest.mark.parametrize("value", [None, "0.25", -1, float("nan"), float("inf"), True, 1001, 1e-31])
def test_invalid_billed_cost_is_unpriced(value: object) -> None:
    usage = _extract_usage(SimpleNamespace(usage=SimpleNamespace(cost=value)))
    assert usage.cost_usd is None


@pytest.mark.parametrize("value", [0, 0.125, 1e-30, 1000])
def test_numeric_billed_cost_including_zero(value: int | float) -> None:
    response = litellm.ModelResponse(usage={"cost": value})
    assert _extract_usage(response).cost_usd == Decimal(str(value))


def test_missing_cost_never_uses_local_estimate() -> None:
    response = litellm.ModelResponse(usage={"prompt_tokens": 4})
    response._hidden_params["response_cost"] = 99
    assert _extract_usage(response).cost_usd is None


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "model,base,is_openrouter",
    [
        ("openrouter/openai/test", None, True),
        ("openai/test", "https://openrouter.ai/api/v1", True),
        ("openai/test", "https://elsewhere.invalid/v1", False),
        ("openrouter/openai/test", "https://elsewhere.invalid/v1", False),
    ],
)
async def test_billed_cost_and_provider_accounting_flag(
    monkeypatch: pytest.MonkeyPatch,
    streamed: bool,
    model: str,
    base: str | None,
    is_openrouter: bool,
) -> None:
    calls: list[dict[str, Any]] = []

    async def parts():
        yield litellm.ModelResponse(stream=True, choices=[], usage={"cost": 0.125})

    async def complete(**kwargs: Any):
        calls.append(kwargs)
        return parts() if streamed else litellm.ModelResponse(usage={"cost": 0.125})

    monkeypatch.setattr(litellm, "acompletion", complete)
    gateway = LLMGateway(_settings())
    if streamed:
        events = [e async for e in gateway.stream_tools([], tools=[], model=model, api_base=base)]
        usage = events[-1].usage
    else:
        usage = (await gateway.chat([], model=model, api_base=base)).usage
    assert usage is not None and usage.cost_usd == Decimal("0.125")
    assert calls[0].get("extra_body", {}).get("usage") == (
        {"include": True} if is_openrouter else None
    )


async def test_stream_recovers_cost_from_litellm_original_usage_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Wrapper:
        def __init__(self):
            self.chunks = [litellm.ModelResponse(stream=True, usage={"cost": 0})]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if hasattr(self, "finished"):
                raise StopAsyncIteration
            self.finished = True
            return litellm.ModelResponse(stream=True, usage={"prompt_tokens": 2})

    async def complete(**kwargs: Any):
        return Wrapper()

    monkeypatch.setattr(litellm, "acompletion", complete)
    events = [e async for e in LLMGateway(_settings()).stream_tools([], tools=[])]
    assert events[-1].usage.cost_usd == Decimal(0)


def test_mixed_usage_and_failover_reset() -> None:
    usage = _Usage()
    usage.add(TokenUsage(cost_usd=Decimal("0.125")))
    usage.add(TokenUsage())
    first = usage.snapshot_and_reset()
    assert first.cost_usd == Decimal("0.125") and first.unpriced_calls == 1
    assert usage.cost_usd is None and usage.unpriced_calls == 0
    usage.add(TokenUsage(cost_usd=Decimal(0)))
    route = _RouteState(model="winner", route=ModelRoute(model="winner"), spent=[("first", first)])
    total = _answer_usage_totals(route, usage)
    assert total.cost_usd == Decimal("0.125") and total.unpriced_calls == 1


@pytest.mark.parametrize(
    "cost_fields",
    [{}, {"costUsd": None, "unpricedCalls": 1}, {"costUsd": "0.00000001", "unpricedCalls": 0}],
)
def test_done_cost_contract_is_additive(cost_fields: dict[str, object]) -> None:
    schema = json.loads(
        (Path(__file__).parents[2] / "contracts/websocket-envelopes.schema.json").read_text()
    )
    Draft202012Validator({"$defs": schema["$defs"], "$ref": "#/$defs/ChatDoneData"}).validate(
        {
            "messageId": str(uuid4()),
            "finishReason": "stop",
            "citationCount": 0,
            "usage": cost_fields,
        }
    )


@pytest.mark.parametrize(
    "cost_fields", [{"costUsd": -1}, {"costUsd": "NaN"}, {"costUsd": "-0.1"}, {"unpricedCalls": -1}]
)
def test_done_cost_contract_rejects_malformed(cost_fields: dict[str, object]) -> None:
    schema = json.loads(
        (Path(__file__).parents[2] / "contracts/websocket-envelopes.schema.json").read_text()
    )
    assert not Draft202012Validator(
        {"$defs": schema["$defs"], "$ref": "#/$defs/ChatDoneData"}
    ).is_valid(
        {
            "messageId": str(uuid4()),
            "finishReason": "stop",
            "citationCount": 0,
            "usage": cost_fields,
        }
    )


async def _run(cost_ctx: _Ctx, gateway: object, **options: Any) -> list[dict[str, Any]]:
    backplane = InMemoryBackplane()
    stream_id = uuid4().hex
    runtime = _runtime(
        cost_ctx, gateway=gateway, retrieval=_FakeRetrieval([]), backplane=backplane, **options
    )
    await runtime.run(
        stream_id=stream_id,
        session_id=cost_ctx.session_id,
        question="q",
        model=_PRIMARY,
        history=[],
        collection_ids=None,
    )
    return [e async for e in backplane.subscribe(stream_id)]


async def test_runtime_cost_persistence_done_telemetry_and_tenant_scope(
    cost_ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.chat_runtime as runtime_module
    from tests.test_chat_runtime import _KpiLogRecorder

    logger = _KpiLogRecorder()
    monkeypatch.setattr(runtime_module, "log", logger)
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(text="Answer"),
                StreamEvent(finish_reason="stop", usage=TokenUsage(cost_usd=Decimal("0.125"))),
            ]
        ]
    )
    events = await _run(cost_ctx, gateway)
    assert events[-1]["data"]["usage"]["costUsd"] == "0.125"
    assert events[-1]["data"]["usage"]["unpricedCalls"] == 0
    async with cost_ctx.sessionmaker() as session:
        rows = await LlmUsageRepository(session, cost_ctx.tenant_id).list_for_session(
            cost_ctx.session_id
        )
        assert rows[0].cost_usd == Decimal("0.125") and rows[0].unpriced_calls == 0
        assert (
            await LlmUsageRepository(session, uuid4()).list_for_session(cost_ctx.session_id) == []
        )
    kpi = logger.kpis()[0]
    assert kpi["cost_usd"] == "0.125" and kpi["unpriced_calls"] == 0


async def test_runtime_success_without_usage_is_one_unpriced_call(cost_ctx: _Ctx) -> None:
    events = await _run(
        cost_ctx,
        _ScriptedGateway([[StreamEvent(text="Answer"), StreamEvent(finish_reason="stop")]]),
    )
    assert events[-1]["data"]["usage"]["costUsd"] is None
    assert events[-1]["data"]["usage"]["unpricedCalls"] == 1
    async with cost_ctx.sessionmaker() as session:
        row = (
            await LlmUsageRepository(session, cost_ctx.tenant_id).list_for_session(
                cost_ctx.session_id
            )
        )[0]
        assert row.cost_usd is None and row.unpriced_calls == 1


async def test_runtime_failover_cost_rows_include_failed_requests(cost_ctx: _Ctx) -> None:
    from app.llm.calls import mark_dispatched

    class Gateway:
        primary_started = False

        async def stream_tools(self, *args: Any, **kwargs: Any):
            mark_dispatched()
            if kwargs["model"] == _PRIMARY:
                if self.primary_started:
                    raise _retryable()
                self.primary_started = True
                yield StreamEvent(
                    tool_calls=(
                        ToolCall(id="search", name="search_text", arguments={"query": "q"}),
                    ),
                    finish_reason="tool_calls",
                    usage=TokenUsage(cost_usd=Decimal("0.125")),
                )
            else:
                yield StreamEvent(text="Answer")
                yield StreamEvent(finish_reason="stop", usage=TokenUsage(cost_usd=Decimal("0.25")))

    fallback = "openrouter/openai/fallback"
    await _set_fallbacks(cost_ctx, [fallback])
    gateway = Gateway()

    async def no_wait(seconds: float) -> None:
        pass

    events = await _run(cost_ctx, gateway, retry_sleep=no_wait)
    assert events[-1]["data"]["usage"]["unpricedCalls"] == 3
    assert events[-1]["data"]["usage"]["costUsd"] == "0.375"
    async with cost_ctx.sessionmaker() as session:
        rows = await LlmUsageRepository(session, cost_ctx.tenant_id).list_for_session(
            cost_ctx.session_id
        )
        by_model = {row.model: row for row in rows}
        assert by_model[_PRIMARY].cost_usd == Decimal("0.125")
        assert by_model[_PRIMARY].unpriced_calls == 3
        assert (
            by_model[fallback].cost_usd == Decimal("0.25")
            and by_model[fallback].unpriced_calls == 0
        )


@pytest.mark.parametrize("cancel", [False, True])
async def test_dispatched_failure_without_tokens_is_salvaged(cost_ctx: _Ctx, cancel: bool) -> None:
    from app.llm.calls import mark_dispatched

    entered = asyncio.Event()
    release = asyncio.Event()

    class Gateway:
        async def stream_tools(self, *args: Any, **kwargs: Any):
            mark_dispatched()
            entered.set()
            await release.wait()
            raise RuntimeError("provider failed")
            yield

    task = asyncio.create_task(_run(cost_ctx, Gateway()))
    await entered.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        await task
    async with cost_ctx.sessionmaker() as session:
        rows = await LlmUsageRepository(session, cost_ctx.tenant_id).list_for_session(
            cost_ctx.session_id
        )
        assert len(rows) == 1 and rows[0].cost_usd is None and rows[0].unpriced_calls == 1


async def test_local_preflight_refusal_does_not_create_spend(cost_ctx: _Ctx) -> None:
    await _run(cost_ctx, LLMGateway(_settings(OPENROUTER_API_KEY="")))
    async with cost_ctx.sessionmaker() as session:
        assert (
            await LlmUsageRepository(session, cost_ctx.tenant_id).list_for_session(
                cost_ctx.session_id
            )
            == []
        )


async def test_summary_spend_records_cost_without_tokens(cost_ctx: _Ctx) -> None:
    from app.tasks.summarize import _record_summary_spend

    async with cost_ctx.sessionmaker() as session:
        await _record_summary_spend(
            session,
            cost_ctx.tenant_id,
            cost_ctx.session_id,
            Completion(content="summary", model="summary", usage=TokenUsage(cost_usd=Decimal(0))),
            ModelRoute(model="summary"),
        )
        rows = await LlmUsageRepository(session, cost_ctx.tenant_id).list_for_session(
            cost_ctx.session_id
        )
        assert len(rows) == 1 and rows[0].cost_usd == Decimal(0) and rows[0].unpriced_calls == 0


async def test_suggestions_have_separate_priced_row(cost_ctx: _Ctx) -> None:
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(text="Answer"),
                StreamEvent(finish_reason="stop", usage=TokenUsage(cost_usd=Decimal("0.125"))),
            ]
        ],
        chat_completion=Completion(
            content='["What next?"]',
            model="suggestions",
            usage=TokenUsage(cost_usd=Decimal("0.25")),
        ),
    )
    events = await _run(cost_ctx, gateway, suggestions_enabled=True)
    done = next(e for e in events if e["type"] == "done")
    assert done["data"]["usage"]["costUsd"] == "0.125"
    async with cost_ctx.sessionmaker() as session:
        rows = await LlmUsageRepository(session, cost_ctx.tenant_id).list_for_session(
            cost_ctx.session_id
        )
        assert len(rows) == 2
        assert next(r for r in rows if r.message_id is None).cost_usd == Decimal("0.25")


async def test_accounting_flag_merges_cache_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_llm_tool_calling import _CACHE_IDS

    calls: list[dict[str, Any]] = []

    async def parts():
        yield litellm.ModelResponse(stream=True, usage={"cost": 0})

    async def complete(**kwargs: Any):
        calls.append(kwargs)
        return parts()

    monkeypatch.setattr(litellm, "acompletion", complete)
    gateway = LLMGateway(_settings())
    _ = [e async for e in gateway.stream_tools([], tools=[], **_CACHE_IDS)]
    assert calls[0]["extra_body"]["usage"] == {"include": True}
    assert "session_id" in calls[0]["extra_body"]
    assert "prompt_cache_key" in calls[0]["extra_body"]


async def test_cancelled_suggestion_is_recorded_separately(cost_ctx: _Ctx) -> None:
    from app.llm.calls import mark_dispatched

    entered = asyncio.Event()
    never = asyncio.Event()

    class Gateway(_ScriptedGateway):
        async def chat(self, *args: Any, **kwargs: Any):
            mark_dispatched()
            entered.set()
            await never.wait()

    gateway = Gateway([[StreamEvent(text="Answer"), StreamEvent(finish_reason="stop")]])
    runtime = _runtime(
        cost_ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=InMemoryBackplane(),
        suggestions_enabled=True,
    )
    task = asyncio.create_task(
        runtime._run_followup_suggestions(
            session_id=cost_ctx.session_id,
            assistant_message_id=uuid4(),
            answer_route=ModelRoute(model="suggestions"),
            question="q",
            answer_text="a",
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with cost_ctx.sessionmaker() as session:
        rows = await LlmUsageRepository(session, cost_ctx.tenant_id).list_for_session(
            cost_ctx.session_id
        )
        assert len(rows) == 1 and rows[0].unpriced_calls == 1 and rows[0].message_id is None


async def test_embedding_batch_exposes_cost_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.llm.embedding_clients as clients

    calls: list[dict[str, Any]] = []

    async def embed(**kwargs: Any):
        calls.append(kwargs)
        return SimpleNamespace(
            data=[{"index": 0, "embedding": [1.0, 0.0]}, {"index": 1, "embedding": [0.0, 1.0]}],
            usage=litellm.Usage(prompt_tokens=4, cost=0.125),
        )

    monkeypatch.setattr(litellm, "aembedding", embed)
    monkeypatch.setattr(clients, "embedding_client", lambda **kwargs: None)
    gateway = LLMGateway(_settings(LLM_EMBEDDING_DIMENSIONS="2"))
    result = await gateway.embed(
        ["a", "b"], model="openai/test", api_base="https://openrouter.ai/api/v1"
    )
    assert result[0].usage.cost_usd == Decimal("0.125")
    assert result[1].usage is None
    assert calls[0]["extra_body"]["usage"] == {"include": True}


def test_accumulation_preserves_smallest_supported_charge() -> None:
    usage = _Usage()
    usage.add(TokenUsage(cost_usd=Decimal("1000")))
    usage.add(TokenUsage(cost_usd=Decimal("1e-30")))
    assert format(usage.cost_usd, "f") == "1000.000000000000000000000000000001"


async def test_failed_summary_dispatch_records_unpriced_and_audit(
    summary_ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.repositories import AuditEventRepository
    from app.llm.calls import mark_dispatched
    from app.tasks import summarize
    from tests.test_session_summary import _summary_settings

    class Gateway:
        def __init__(self, settings):
            pass

        async def chat(self, *args: Any, **kwargs: Any):
            mark_dispatched()
            raise RuntimeError("request failed")

    monkeypatch.setattr(summarize, "LLMGateway", Gateway)
    monkeypatch.setattr(summarize, "get_settings", lambda: _summary_settings(keep=4, min_batch=4))
    with pytest.raises(RuntimeError, match="request failed"):
        await summarize._summarize(summary_ctx.tenant_id, summary_ctx.session_id)
    async with summary_ctx.sessionmaker() as session:
        rows = await LlmUsageRepository(session, summary_ctx.tenant_id).list_for_session(
            summary_ctx.session_id
        )
        assert len(rows) == 1 and rows[0].unpriced_calls == 1 and rows[0].cost_usd is None
        events = await AuditEventRepository(session, summary_ctx.tenant_id).list_recent(limit=100)
        assert any(
            e.action == "session.summarized"
            and e.metadata.get("refused") == "provider_request_incomplete"
            for e in events
        )


async def test_pinned_litellm_stream_retains_original_provider_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
    from openai.types.chat import ChatCompletionChunk

    async def raw_chunks():
        yield ChatCompletionChunk(
            id="fake",
            object="chat.completion.chunk",
            created=0,
            model="gpt-4o-mini",
            choices=[{"index": 0, "delta": {"content": "answer"}, "finish_reason": None}],
        )
        yield ChatCompletionChunk(
            id="fake",
            object="chat.completion.chunk",
            created=0,
            model="gpt-4o-mini",
            choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
        )
        yield ChatCompletionChunk(
            id="fake",
            object="chat.completion.chunk",
            created=0,
            model="gpt-4o-mini",
            choices=[],
            usage={"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5, "cost": 0.125},
        )

    wrapper = CustomStreamWrapper(
        completion_stream=raw_chunks(),
        model="gpt-4o-mini",
        custom_llm_provider="openai",
        stream_options={"include_usage": True},
        logging_obj=SimpleNamespace(
            model_call_details={},
            optional_params={},
            messages=[],
            _llm_caching_handler=None,
            success_handler=lambda *a, **kw: None,
            failure_handler=lambda *a, **kw: None,
            async_success_handler=AsyncMock(),
            async_failure_handler=AsyncMock(),
        ),
    )

    async def complete(**kwargs: Any):
        return wrapper

    monkeypatch.setattr(litellm, "acompletion", complete)
    events = [e async for e in LLMGateway(_settings()).stream_tools([], tools=[])]
    assert events[-1].usage.cost_usd == Decimal("0.125")
    assert events[-1].usage.total_tokens == 5


async def test_gateway_midstream_failure_counts_one_unpriced_call(
    cost_ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def parts():
        yield litellm.ModelResponse(
            stream=True, choices=[{"delta": {"content": "partial"}, "index": 0}]
        )
        raise RuntimeError("upstream failure")

    async def complete(**kwargs: Any):
        return parts()

    monkeypatch.setattr(litellm, "acompletion", complete)
    events = await _run(cost_ctx, LLMGateway(_settings()))
    assert events[-1]["type"] == "error"
    async with cost_ctx.sessionmaker() as session:
        rows = await LlmUsageRepository(session, cost_ctx.tenant_id).list_for_session(
            cost_ctx.session_id
        )
        assert len(rows) == 1 and rows[0].unpriced_calls == 1 and rows[0].cost_usd is None


async def test_plain_stream_exposes_billed_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    async def parts():
        yield litellm.ModelResponse(stream=True, choices=[], usage={"cost": 0})

    async def complete(**kwargs: Any):
        assert kwargs["extra_body"]["usage"] == {"include": True}
        assert kwargs["stream_options"]["include_usage"] is True
        return parts()

    monkeypatch.setattr(litellm, "acompletion", complete)
    chunks = [chunk async for chunk in LLMGateway(_settings()).stream([])]
    assert len(chunks) == 1 and chunks[0].usage.cost_usd == Decimal(0)


async def test_local_wire_failure_is_not_a_dispatched_call(
    cost_ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def invalid_wire(messages):
        raise ValueError("local serialization failure")

    async def complete(**kwargs: Any):
        nonlocal called
        called = True
        raise AssertionError("local failure must precede provider I/O")

    monkeypatch.setattr(LLMGateway, "_to_wire_messages", staticmethod(invalid_wire))
    monkeypatch.setattr(litellm, "acompletion", complete)
    events = await _run(cost_ctx, LLMGateway(_settings()))
    assert events[-1]["type"] == "error" and not called
    async with cost_ctx.sessionmaker() as session:
        rows = await LlmUsageRepository(session, cost_ctx.tenant_id).list_for_session(
            cost_ctx.session_id
        )
        assert rows == []
