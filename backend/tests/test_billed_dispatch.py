"""Real-adapter dispatch accounting regressions for #832 (PR #835 R1)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import litellm
import pytest
from litellm.caching.in_memory_cache import InMemoryCache
from openai import AsyncOpenAI

from app.core.errors import DependencyError, ValidationError
from app.db.repositories import LlmUsageRepository
from app.llm.calls import begin_call
from app.llm.gateway import LLMGateway
from app.services.provider_models import ModelRoute
from tests.test_billed_cost import _run
from tests.test_chat_runtime import _Ctx
from tests.test_chat_runtime import ctx as _runtime_ctx
from tests.test_llm_tool_calling import _settings

cost_ctx = _runtime_ctx


@pytest.fixture
async def provider_transport(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncIterator[list[httpx.Request]]:
    """Keep the real native adapter/client cache; replace HTTP transport only."""
    requests: list[httpx.Request] = []
    cache = InMemoryCache()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            if failure == "503":
                return httpx.Response(
                    503, headers={"retry-after": "0"}, json={"error": {"message": "unavailable"}}
                )
            raise httpx.ReadTimeout("response lost after dispatch", request=request)
        body = {
            "id": "offline",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "test",
            "choices": [{"index": 0, "delta": {"content": "Answer"}, "finish_reason": "stop"}],
        }
        usage = {
            **body,
            "choices": [],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3, "cost": 0.125},
        }
        if not json.loads(request.content).get("stream"):
            return httpx.Response(
                200,
                json={
                    **usage,
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Answer"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"data: {json.dumps(body)}\n\ndata: {json.dumps(usage)}\n\ndata: [DONE]\n\n",
        )

    async def no_retry_wait(self: Any, **kwargs: Any) -> None:
        pass

    failure = getattr(request, "param", "timeout")
    monkeypatch.setattr(AsyncOpenAI, "_sleep_for_retry", no_retry_wait)
    monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", cache)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(litellm, "aclient_session", transport)
        try:
            yield requests
        finally:
            for client in list(cache.cache_dict.values()):
                await client.close()


@pytest.mark.parametrize("provider_transport", ["timeout", "503"], indirect=True)
async def test_sdk_retry_is_accounted_by_runtime(
    cost_ctx: _Ctx, provider_transport: list[httpx.Request]
) -> None:
    async def no_wait(seconds: float) -> None:
        pass

    async def route(session: Any, model: str) -> ModelRoute:
        return ModelRoute(model="openrouter/openai/test")

    events = await _run(
        cost_ctx, LLMGateway(_settings()), retry_sleep=no_wait, model_route_resolver=route
    )
    assert len(provider_transport) == 2
    done = next(e for e in events if e["type"] == "done")
    assert done["data"]["usage"]["costUsd"] == "0.125"
    assert done["data"]["usage"]["unpricedCalls"] == 1
    async with cost_ctx.sessionmaker() as session:
        rows = await LlmUsageRepository(session, cost_ctx.tenant_id).list_for_session(
            cost_ctx.session_id
        )
        assert len(rows) == 1
        assert rows[0].cost_usd == Decimal("0.125")
        assert rows[0].unpriced_calls == 1


@pytest.mark.parametrize("method", ["chat", "stream", "stream_tools"])
async def test_gateway_does_not_retry_inside_one_accounted_call(
    provider_transport: list[httpx.Request], method: str
) -> None:
    gateway = LLMGateway(_settings())
    tracker = begin_call()
    try:
        with pytest.raises(DependencyError):
            result = getattr(gateway, method)(
                [],
                model="openrouter/openai/test",
                **({"tools": []} if method == "stream_tools" else {}),
            )
            if method in {"stream", "stream_tools"}:
                _ = [event async for event in result]
            else:
                await result
        assert len(provider_transport) == 1
        assert tracker.dispatched
    finally:
        tracker.finish()


@pytest.mark.parametrize("method", ["chat", "stream", "stream_tools", "embed"])
async def test_real_adapter_local_rejection_is_not_dispatched(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    requests: list[httpx.Request] = []

    async def forbidden_send(self: httpx.AsyncClient, request: httpx.Request, **kwargs: Any) -> Any:
        requests.append(request)
        raise AssertionError("local validation must precede HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden_send)
    gateway = LLMGateway(_settings())
    tracker = begin_call()
    try:
        with pytest.raises(ValidationError):
            result = getattr(gateway, method)(
                [],
                model="invalid-review-provider/model",
                **({"tools": []} if method == "stream_tools" else {}),
            )
            if method in {"stream", "stream_tools"}:
                _ = [event async for event in result]
            else:
                await result
        assert requests == []
        assert not tracker.dispatched
    finally:
        tracker.finish()


async def test_runtime_local_adapter_rejection_has_no_spend_row(
    cost_ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []

    async def forbidden_send(self: httpx.AsyncClient, request: httpx.Request, **kwargs: Any) -> Any:
        requests.append(request)
        raise AssertionError("local validation must precede HTTP")

    async def route(session: Any, model: str) -> ModelRoute:
        return ModelRoute(model="invalid-review-provider/model")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden_send)
    events = await _run(cost_ctx, LLMGateway(_settings()), model_route_resolver=route)
    assert events[-1]["type"] == "error"
    assert requests == []
    async with cost_ctx.sessionmaker() as session:
        rows = await LlmUsageRepository(session, cost_ctx.tenant_id).list_for_session(
            cost_ctx.session_id
        )
        assert rows == []
