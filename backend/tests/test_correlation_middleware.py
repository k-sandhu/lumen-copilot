"""Correlation error-path memory streams have a proven owner (#803)."""

from __future__ import annotations

import asyncio
import traceback
import uuid
from typing import Any

import anyio
import httpx
import pytest
import structlog
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from app.main import CorrelationMiddleware


def test_unhandled_http_error_closes_middleware_owned_streams(monkeypatch: Any) -> None:
    streams: list[Any] = []
    owners: list[str] = []
    original = anyio.create_memory_object_stream

    def tracked(*args: Any, **kwargs: Any) -> Any:
        pair = original(*args, **kwargs)
        streams.extend(pair)
        owners.extend(frame.filename.replace("\\", "/") for frame in traceback.extract_stack())
        return pair

    monkeypatch.setattr(anyio, "create_memory_object_stream", tracked)
    application = FastAPI()
    application.add_middleware(CorrelationMiddleware)

    @application.get("/unexpected")
    async def unexpected() -> None:
        raise ValueError("synthetic failure")

    try:
        with TestClient(application, raise_server_exceptions=False) as client:
            response = client.get("/unexpected")
        assert response.status_code == 500
        assert all(stream._closed for stream in streams), {
            "unclosed_types": [type(stream).__name__ for stream in streams if not stream._closed],
            "base_middleware_allocated": any(
                name.endswith("/starlette/middleware/base.py") for name in owners
            ),
        }
    finally:
        # Close the instrumentation's retained references even when the red
        # assertion fails, so its proven ownership isn't obscured by GC timing.
        for stream in streams:
            stream.close()


async def test_concurrent_requests_keep_state_headers_and_context_isolated() -> None:
    application = FastAPI()
    application.add_middleware(CorrelationMiddleware)
    arrivals = 0
    ready = asyncio.Event()

    @application.get("/context")
    async def context(request: Request) -> dict[str, Any]:
        nonlocal arrivals
        before = structlog.contextvars.get_contextvars()
        arrivals += 1
        if arrivals == 2:
            ready.set()
        await ready.wait()
        assert structlog.contextvars.get_contextvars() == before
        return {"context": before, "request_state_id": request.state.request_id}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            *(
                client.get(
                    "/context", headers={"x-request-id": request_id, "x-tenant-id": tenant_id}
                )
                for request_id, tenant_id in [("request-a", "tenant-a"), ("request-b", "tenant-b")]
            )
        )
    for response, request_id, tenant_id in zip(
        responses, ["request-a", "request-b"], ["tenant-a", "tenant-b"], strict=True
    ):
        assert response.headers["x-request-id"] == request_id
        assert response.json() == {
            "request_state_id": request_id,
            "context": {
                "request_id": request_id,
                "tenant_id": tenant_id,
                "path": "/context",
                "method": "GET",
            },
        }
    assert structlog.contextvars.get_contextvars() == {}


def test_missing_id_is_minted_and_unknown_route_still_has_correlation() -> None:
    application = FastAPI()
    application.add_middleware(CorrelationMiddleware)
    with TestClient(application) as client:
        response = client.get("/missing")
    assert response.status_code == 404
    assert uuid.UUID(hex=response.headers["x-request-id"])


@pytest.mark.parametrize("error", [ValueError("synthetic"), asyncio.CancelledError()])
async def test_exception_and_cancellation_clear_http_context(error: BaseException) -> None:
    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        assert structlog.contextvars.get_contextvars()["request_id"] == "failed-request"
        raise error

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        raise AssertionError("failed app must not emit a response")

    scope: Scope = {
        "type": "http",
        "path": "/failure",
        "method": "GET",
        "headers": [(b"x-request-id", b"failed-request")],
    }
    with pytest.raises(type(error)):
        await CorrelationMiddleware(application)(scope, receive, send)
    assert structlog.contextvars.get_contextvars() == {}


@pytest.mark.parametrize("scope_type", ["websocket", "lifespan"])
async def test_non_http_is_passed_through_without_changing_context(scope_type: str) -> None:
    received: list[Scope] = []
    scope: Scope = {"type": scope_type}
    structlog.contextvars.bind_contextvars(existing="untouched")

    async def application(actual: Scope, receive: Receive, send: Send) -> None:
        received.append(actual)
        assert structlog.contextvars.get_contextvars() == {"existing": "untouched"}

    async def receive() -> Message:
        raise AssertionError("not used")

    async def send(message: Message) -> None:
        raise AssertionError("not used")

    try:
        await CorrelationMiddleware(application)(scope, receive, send)
        assert received == [scope]
        assert received[0] is scope
        assert structlog.contextvars.get_contextvars() == {"existing": "untouched"}
    finally:
        structlog.contextvars.clear_contextvars()
