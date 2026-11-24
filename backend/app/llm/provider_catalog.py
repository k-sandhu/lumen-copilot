"""OpenAI-compatible provider probes: model-catalog discovery + connection test.

ADR-0004: HTTP to an LLM provider lives in ``app/llm/`` (the single owning module),
exposing domain types — not in ``services/``. Both probes go through the shared
SSRF egress guard (resolve-all / reject-any + IP-pin, TOCTOU defence), send the
API key as a Bearer token when present, and bound the response size.

**Error boundary (spec 0029, issue #585).** Nothing vendor-shaped leaves this
module: every transport / HTTP / shape failure is translated to a typed
:class:`ProviderProbeError` carrying a stage (``discovery`` /
``connection_test``), a stable safe ``code`` and ``retryable``. Its string form
is a fixed safe message — never exception text, a resolved IP, or an external
documentation link — so callers may persist/surface it directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from contextvars import ContextVar
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.net.egress import EgressBlockedError, pin_url_to_ip, resolve_safe_ip

_MAX_MODELS = 1000

_PROBING: ContextVar[bool] = ContextVar("provider_probe", default=False)


class _ProbeLogFilter(logging.Filter):
    """Remove transport URLs/addresses from library logs in this probe's context."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _PROBING.get():
            record.msg = "Provider probe HTTP activity."
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


for _logger_name in (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
):
    logging.getLogger(_logger_name).addFilter(_ProbeLogFilter())

#: Hard bound on a probe response body so a pathological provider cannot make
#: the backend buffer an unbounded payload (issue #585).
MAX_RESPONSE_BYTES = 1024 * 1024

#: Fixed safe messages per code. These are the ONLY strings a probe failure may
#: surface — persisted as ``last_error`` and shown to admins.
SAFE_MESSAGES: dict[str, str] = {
    "blocked_address": "The provider address is blocked by the egress policy.",
    "dns_failed": "The provider host could not be resolved.",
    "timeout": "The provider did not respond in time.",
    "connection_failed": "Could not connect to the provider.",
    "unauthorized": "The provider rejected the API key (401).",
    "forbidden": "The provider refused access with the configured key (403).",
    "not_found": "The provider endpoint or model was not found (404).",
    "rate_limited": "The provider rate-limited the request (429).",
    "provider_error": "The provider returned a server error (5xx).",
    "unexpected_status": "The provider returned an unexpected status.",
    "invalid_response": "The provider returned an unexpected response shape.",
    "response_too_large": "The provider response was too large.",
    "validation_failed": "Provider validation failed.",
}

_RETRYABLE_CODES = frozenset(
    {"dns_failed", "timeout", "connection_failed", "rate_limited", "provider_error"}
)


def safe_message(code: str) -> str:
    """The fixed safe message for a probe failure code."""
    return SAFE_MESSAGES.get(code, SAFE_MESSAGES["validation_failed"])


class ProviderProbeError(Exception):
    """A typed, safe provider-probe failure (spec 0029).

    ``str(err)`` is the fixed safe message for ``code``. Provider exceptions
    are suppressed at this boundary; operators receive safe correlated codes.
    """

    def __init__(self, *, stage: str, code: str) -> None:
        self.stage = stage if stage in {"config", "discovery", "connection_test"} else "config"
        self.code = code if code in SAFE_MESSAGES else "validation_failed"
        self.retryable = self.code in _RETRYABLE_CODES
        super().__init__(safe_message(self.code))

    @property
    def safe_message(self) -> str:
        return safe_message(self.code)


def _status_code(http_status: int) -> str:
    if http_status == 401:
        return "unauthorized"
    if http_status == 403:
        return "forbidden"
    if http_status == 404:
        return "not_found"
    if http_status == 429:
        return "rate_limited"
    if 500 <= http_status <= 599:
        return "provider_error"
    return "unexpected_status"


def _resolve_pinned(url: str, *, stage: str) -> tuple[str, str, str]:
    """Resolve + IP-pin ``url``; return ``(pinned_url, host, netloc)``."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
        if (
            parts.scheme != "https"
            or not host
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or port == 0
        ):
            raise ProviderProbeError(stage=stage, code="blocked_address")
        safe_ip = resolve_safe_ip(host)  # resolve-all, reject-any (raises on block)
    except EgressBlockedError as exc:
        code = "dns_failed" if isinstance(exc.__cause__, socket.gaierror) else "blocked_address"
        raise ProviderProbeError(stage=stage, code=code) from None
    except ValueError:
        raise ProviderProbeError(stage=stage, code="blocked_address") from None
    return pin_url_to_ip(url, safe_ip), host, parts.netloc


async def _probe(
    *,
    stage: str,
    method: str,
    url: str,
    api_key: str | None,
    timeout_seconds: float,
    user_agent: str,
    http_client: httpx.AsyncClient | None,
    json_body: dict[str, object] | None = None,
) -> object:
    """One SSRF-guarded probe request; return the decoded JSON body.

    Raises :class:`ProviderProbeError` for every failure class.
    """
    headers: dict[str, str] = {
        "User-Agent": user_agent,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    owns_client = http_client is None
    client = http_client
    token = _PROBING.set(True)
    try:
        if client is None:
            client = httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)
        # A total deadline also bounds a server that drips data forever while
        # staying inside the individual read timeout. Never buffer an unbounded
        # response before checking its size (including decompressed bytes).
        async with asyncio.timeout(timeout_seconds):
            pinned, host, netloc = _resolve_pinned(url, stage=stage)
            async with client.stream(
                method,
                pinned,
                headers={**headers, "Host": netloc},
                extensions={"sni_hostname": host},
                json=json_body,
                follow_redirects=False,
                timeout=timeout_seconds,
            ) as response:
                if not (200 <= response.status_code <= 299):
                    raise ProviderProbeError(stage=stage, code=_status_code(response.status_code))
                # Compression could expand an entire chunk before the byte-limit
                # check. Probes request identity and reject an ignored negotiation.
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ProviderProbeError(stage=stage, code="invalid_response")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ProviderProbeError(stage=stage, code="response_too_large")
                    content.extend(chunk)
                try:
                    return json.loads(content)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    raise ProviderProbeError(stage=stage, code="invalid_response") from None
    except (TimeoutError, httpx.TimeoutException):
        raise ProviderProbeError(stage=stage, code="timeout") from None
    except httpx.HTTPError:
        raise ProviderProbeError(stage=stage, code="connection_failed") from None
    except ProviderProbeError:
        raise
    except Exception:
        raise ProviderProbeError(stage=stage, code="validation_failed") from None
    finally:
        try:
            if owns_client and client is not None:
                await client.aclose()
        except Exception:
            raise ProviderProbeError(stage=stage, code="validation_failed") from None
        finally:
            _PROBING.reset(token)


def _models_url(base_url: str) -> str:
    """Build the ``{base_url}/models`` discovery URL, tolerant of a trailing slash."""
    try:
        parts = urlsplit(base_url)
    except ValueError:
        raise ProviderProbeError(stage="discovery", code="blocked_address") from None
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, f"{path}/models", parts.query, parts.fragment))


def _chat_url(base_url: str) -> str:
    """Build the ``{base_url}/chat/completions`` test URL."""
    try:
        parts = urlsplit(base_url)
    except ValueError:
        raise ProviderProbeError(stage="connection_test", code="blocked_address") from None
    path = parts.path.rstrip("/")
    return urlunsplit(
        (parts.scheme, parts.netloc, f"{path}/chat/completions", parts.query, parts.fragment)
    )


def _map_models(payload: object) -> list[dict[str, object]]:
    """Map an OpenAI ``/models`` response to the ``discovered_models`` snapshot."""
    if not isinstance(payload, dict):
        raise ProviderProbeError(stage="discovery", code="invalid_response")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ProviderProbeError(stage="discovery", code="invalid_response")

    models: list[dict[str, object]] = []
    for entry in data[:_MAX_MODELS]:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        raw_label = entry.get("name")
        label = raw_label if isinstance(raw_label, str) and raw_label else model_id
        models.append({"id": model_id, "label": label})
    if not models:
        raise ProviderProbeError(stage="discovery", code="invalid_response")
    return models


async def discover_models(
    *,
    base_url: str,
    api_key: str | None,
    timeout_seconds: float,
    user_agent: str,
    http_client: httpx.AsyncClient | None = None,
) -> list[dict[str, object]]:
    """SSRF-guarded OpenAI-compatible ``/models`` discovery (see module docstring).

    Success proves reachability + catalog shape only — never authentication
    (a public model list answers without a valid key; spec 0029).
    """
    payload = await _probe(
        stage="discovery",
        method="GET",
        url=_models_url(base_url),
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        user_agent=user_agent,
        http_client=http_client,
    )
    return _map_models(payload)


async def test_connection(
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    timeout_seconds: float,
    user_agent: str,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """One minimal bounded chat request proving auth + chat capability.

    Sends ``max_tokens: 1`` so the (possibly billed) upstream cost is the
    smallest a chat completion allows. Returns ``None`` on a 2xx; raises
    :class:`ProviderProbeError` (stage ``connection_test``) otherwise.
    """
    payload = await _probe(
        stage="connection_test",
        method="POST",
        url=_chat_url(base_url),
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        user_agent=user_agent,
        http_client=http_client,
        json_body={
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        },
    )
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return
    raise ProviderProbeError(stage="connection_test", code="invalid_response")


__all__ = [
    "MAX_RESPONSE_BYTES",
    "ProviderProbeError",
    "discover_models",
    "safe_message",
    "test_connection",
]
