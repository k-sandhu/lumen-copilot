"""Provider probe error mapping (issue #585, spec 0029).

The ``app/llm`` boundary translates every transport / HTTP / shape failure to a
typed :class:`ProviderProbeError` with a stable safe code, stage and
retryability. Nothing vendor-shaped (exception text, resolved IP, external doc
links) may appear in the error. DNS/SSRF resolution is stubbed at the
``provider_catalog`` import seam; the HTTP leg runs over ``MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket

import httpx
import pytest

from app.llm import provider_catalog
from app.llm.provider_catalog import ProviderProbeError, discover_models
from app.llm.provider_catalog import test_connection as probe_test_connection
from app.net.egress import EgressBlockedError

_BASE = "https://provider.example.com/v1"
_PUBLIC_IP = "93.184.216.34"
_MODELS_OK = {"data": [{"id": "m1", "name": "Model 1"}]}


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_catalog, "resolve_safe_ip", lambda host: _PUBLIC_IP)


def _client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


async def _discover(handler: object) -> list[dict[str, object]]:
    return await discover_models(
        base_url=_BASE,
        api_key="test-key",
        timeout_seconds=5.0,
        user_agent="test",
        http_client=_client(handler),
    )


async def test_discover_success() -> None:
    models = await _discover(lambda req: httpx.Response(200, json=_MODELS_OK))
    assert models == [{"id": "m1", "label": "Model 1"}]


@pytest.mark.parametrize(
    ("status_code", "code", "retryable"),
    [
        (401, "unauthorized", False),
        (403, "forbidden", False),
        (404, "not_found", False),
        (429, "rate_limited", True),
        (500, "provider_error", True),
        (503, "provider_error", True),
        (418, "unexpected_status", False),
        (302, "unexpected_status", False),
    ],
)
async def test_discover_http_status_maps_to_safe_code(
    status_code: int, code: str, retryable: bool
) -> None:
    with pytest.raises(ProviderProbeError) as excinfo:
        await _discover(lambda req: httpx.Response(status_code, json={"error": "x"}))
    err = excinfo.value
    assert err.code == code
    assert err.stage == "discovery"
    assert err.retryable is retryable
    # The string form is the fixed safe message — no IP, no URL, no vendor text.
    assert _PUBLIC_IP not in str(err)
    assert "http" not in str(err)


async def test_discover_timeout_maps_to_timeout() -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(ProviderProbeError) as excinfo:
        await _discover(_handler)
    assert excinfo.value.code == "timeout"
    assert excinfo.value.retryable is True


async def test_discover_connect_error_maps_to_connection_failed() -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(ProviderProbeError) as excinfo:
        await _discover(_handler)
    assert excinfo.value.code == "connection_failed"
    assert excinfo.value.retryable is True


async def test_discover_bad_shape_maps_to_invalid_response() -> None:
    with pytest.raises(ProviderProbeError) as excinfo:
        await _discover(lambda req: httpx.Response(200, json={"nope": 1}))
    assert excinfo.value.code == "invalid_response"


async def test_discover_empty_catalog_maps_to_invalid_response() -> None:
    with pytest.raises(ProviderProbeError) as excinfo:
        await _discover(lambda req: httpx.Response(200, json={"data": []}))
    assert excinfo.value.code == "invalid_response"


async def test_discover_oversized_response_is_bounded() -> None:
    big = b"x" * (provider_catalog.MAX_RESPONSE_BYTES + 1)
    with pytest.raises(ProviderProbeError) as excinfo:
        await _discover(lambda req: httpx.Response(200, content=big))
    assert excinfo.value.code == "response_too_large"


async def test_blocked_address_maps_without_leaking_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _blocked(host: str) -> str:
        raise EgressBlockedError(f"host {host!r} resolves to a blocked address (10.0.0.5); refused")

    monkeypatch.setattr(provider_catalog, "resolve_safe_ip", _blocked)
    with pytest.raises(ProviderProbeError) as excinfo:
        await _discover(lambda req: httpx.Response(200, json=_MODELS_OK))
    err = excinfo.value
    assert err.code == "blocked_address"
    assert err.retryable is False
    assert "10.0.0.5" not in str(err)


async def test_dns_failure_maps_to_dns_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _dns(host: str) -> str:
        raise EgressBlockedError("could not resolve host") from socket.gaierror("nx")

    monkeypatch.setattr(provider_catalog, "resolve_safe_ip", _dns)
    with pytest.raises(ProviderProbeError) as excinfo:
        await _discover(lambda req: httpx.Response(200, json=_MODELS_OK))
    assert excinfo.value.code == "dns_failed"
    assert excinfo.value.retryable is True


async def test_connection_test_success_and_auth_failure() -> None:
    seen: list[httpx.Request] = []

    def _ok(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    await probe_test_connection(
        base_url=_BASE,
        api_key="test-key",
        model="m1",
        timeout_seconds=5.0,
        user_agent="test",
        http_client=_client(_ok),
    )
    assert seen[0].url.path.endswith("/chat/completions")
    assert seen[0].headers.get("authorization") == "Bearer test-key"

    sent = json.loads(seen[0].content)
    assert sent["model"] == "m1"
    assert sent["max_tokens"] == 1  # the bounded minimal request

    with pytest.raises(ProviderProbeError) as excinfo:
        await probe_test_connection(
            base_url=_BASE,
            api_key="bad-key",
            model="m1",
            timeout_seconds=5.0,
            user_agent="test",
            http_client=_client(lambda req: httpx.Response(401, json={"error": "x"})),
        )
    assert excinfo.value.code == "unauthorized"
    assert excinfo.value.stage == "connection_test"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"error": "raw vendor detail"},
        {"choices": []},
        {"choices": [{"message": {"content": None}}]},
    ],
)
async def test_chat_success_requires_a_usable_choice(payload: object) -> None:
    with pytest.raises(ProviderProbeError, match="unexpected response shape") as caught:
        await probe_test_connection(
            base_url=_BASE,
            api_key="test-key",
            model="m1",
            timeout_seconds=5,
            user_agent="test",
            http_client=_client(lambda req: httpx.Response(200, json=payload)),
        )
    assert caught.value.code == "invalid_response"
    assert caught.value.stage == "connection_test"


async def test_body_limit_stops_reading_before_the_rest_of_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_catalog, "MAX_RESPONSE_BYTES", 8)
    consumed: list[int] = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            for index, chunk in enumerate([b"12345678", b"9", b"unread tail"]):
                consumed.append(index)
                yield chunk

    with pytest.raises(ProviderProbeError) as caught:
        await _discover(lambda req: httpx.Response(200, stream=Chunks()))
    assert caught.value.code == "response_too_large"
    assert consumed == [0, 1]


async def test_probe_never_follows_redirects_even_with_injected_client() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        with pytest.raises(ProviderProbeError) as caught:
            await discover_models(
                base_url=_BASE,
                api_key="test-key",
                timeout_seconds=5,
                user_agent="test",
                http_client=client,
            )
    assert caught.value.code == "unexpected_status"
    assert len(seen) == 1


async def test_compressed_body_is_rejected_without_decoding_or_reading() -> None:
    class NeverRead(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("compressed probe body must not be read")
            yield b""  # pragma: no cover

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=NeverRead())

    with pytest.raises(ProviderProbeError) as caught:
        await _discover(handler)
    assert caught.value.code == "invalid_response"


async def test_total_deadline_cancels_a_stream_that_never_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline = asyncio.timeout(None)
    monkeypatch.setattr(provider_catalog.asyncio, "timeout", lambda seconds: deadline)

    class Waiting(httpx.AsyncByteStream):
        async def __aiter__(self):
            # Expire explicitly at this handshake, without sleeps or elapsed-time
            # assertions. A per-read timeout on the injected fake cannot help.
            deadline.reschedule(asyncio.get_running_loop().time() - 1)
            await asyncio.Event().wait()
            yield b""  # pragma: no cover

    with pytest.raises(ProviderProbeError) as caught:
        await _discover(lambda req: httpx.Response(200, stream=Waiting()))
    assert caught.value.code == "timeout"


@pytest.mark.parametrize("status", [200, 401])
async def test_library_request_logs_never_expose_the_pinned_address(
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    with caplog.at_level(logging.DEBUG):
        if status == 200:
            await _discover(lambda req: httpx.Response(status, json=_MODELS_OK))
        else:
            with pytest.raises(ProviderProbeError):
                await _discover(lambda req: httpx.Response(status, json={"error": "vendor body"}))
    assert _PUBLIC_IP not in caplog.text
    assert "test-key" not in caplog.text
    assert "vendor body" not in caplog.text
