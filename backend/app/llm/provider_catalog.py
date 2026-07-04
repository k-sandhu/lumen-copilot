"""OpenAI-compatible provider model-catalog discovery.

ADR-0004: HTTP to an LLM provider lives in ``app/llm/`` (the single owning module),
exposing domain types — not in ``services/``. Fetches ``GET {base_url}/models``
through the shared SSRF egress guard (resolve-all / reject-any + IP-pin, TOCTOU
defence — the same guard the web fetcher / MCP adapter use), sends the API key as a
bearer token when present, and maps the OpenAI response to the ``discovered_models``
snapshot shape. Raises on any transport / status / shape failure so the caller can
record ``status=error`` (never a 500).
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import httpx

from app.net.egress import pin_url_to_ip, resolve_safe_ip

_MAX_MODELS = 1000


def _models_url(base_url: str) -> str:
    """Build the ``{base_url}/models`` discovery URL, tolerant of a trailing slash.

    An OpenAI-compatible base URL is typically ``https://host/v1``; discovery hits
    ``{base}/models``. A trailing slash on the base is normalised so we never emit a
    ``//models`` path.
    """
    parts = urlsplit(base_url)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, f"{path}/models", "", ""))


def _map_models(payload: object) -> list[dict[str, object]]:
    """Map an OpenAI ``/models`` response to the ``discovered_models`` snapshot.

    Expects ``{"data": [{"id": "..."}, ...]}`` (the OpenAI shape). Each entry maps to
    ``{"id": str, "label": str | None}`` with a derived human label (an entry's
    ``name`` if present, else its ``id``). A missing/non-list ``data`` or an entry
    with no ``id`` is a shape error → raised (the caller records ``status=error``).
    """
    if not isinstance(payload, dict):
        raise ValueError("provider /models response was not a JSON object")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("provider /models response had no 'data' list")

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
        raise ValueError("provider /models response listed no usable models")
    return models


async def discover_models(
    *,
    base_url: str,
    api_key: str | None,
    timeout_seconds: float,
    user_agent: str,
    http_client: httpx.AsyncClient | None = None,
) -> list[dict[str, object]]:
    """SSRF-guarded OpenAI-compatible ``/models`` discovery (see module docstring)."""
    url = _models_url(base_url)
    parts = urlsplit(url)
    host = parts.hostname or ""
    safe_ip = resolve_safe_ip(host)  # resolve-all, reject-any (raises on block)
    pinned = pin_url_to_ip(url, safe_ip)
    headers: dict[str, str] = {"User-Agent": user_agent, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)
    try:
        response = await client.get(
            pinned,
            headers={**headers, "Host": parts.netloc},
            extensions={"sni_hostname": host},
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            await client.aclose()

    return _map_models(payload)
