"""Loop-scoped clients for pinned LiteLLM's OpenAI embedding adapter (#780).

Only the gateway consumes the vendor client. Other adapters and caller-owned
HTTP transports keep LiteLLM's existing path; no vendor-global cache is cleared.
"""

from __future__ import annotations

import asyncio
from threading import RLock
from typing import Any
from weakref import WeakKeyDictionary

_clients: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[Any, ...], Any]] = (
    WeakKeyDictionary()
)
_lock = RLock()


def embedding_client(*, model: str, api_key: str, api_base: str | None, timeout: float) -> Any:
    """Return an owned client only when LiteLLM resolves its OpenAI adapter.

    Resolution mirrors the pinned adapter, including native OpenAI without an
    explicit base, organization, legacy LiteLLM/env fallbacks and SDK retries.
    No credentials or cache keys are logged or returned outside app/llm/.
    """
    import litellm
    from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider
    from litellm.secret_managers.main import get_secret_str

    _, provider, dynamic_key, resolved_base = get_llm_provider(
        model=model,
        api_key=api_key,
        api_base=api_base,
    )
    if provider != "openai" or litellm.aclient_session is not None:
        return None
    base = (
        resolved_base
        or litellm.api_base
        or get_secret_str("OPENAI_API_BASE")
        or "https://api.openai.com/v1"
    )
    key = (
        dynamic_key
        or api_key
        or litellm.api_key
        or litellm.openai_key
        or get_secret_str("OPENAI_API_KEY")
    )
    organization = litellm.organization or get_secret_str("OPENAI_ORGANIZATION") or None
    retries: int = litellm.DEFAULT_MAX_RETRIES  # type: ignore[attr-defined]  # vendor re-export
    identity = (key, base, timeout, retries, organization)
    loop = asyncio.get_running_loop()
    with _lock:
        owned = _clients.setdefault(loop, {})
        if identity not in owned:
            from openai import AsyncOpenAI

            owned[identity] = AsyncOpenAI(
                api_key=key,
                base_url=base,
                timeout=timeout,
                max_retries=retries,
                organization=organization,
            )
        return owned[identity]


async def aclose_embedding_clients() -> None:
    """Retire this loop's owned clients before it closes, never another loop's.

    Called after producers drain in app shutdown and from run_task's finally.
    Failed provider/close operations cannot leave a reusable stale registry entry
    or mask the task's original typed failure/cancellation. Borrowed custom HTTP
    transports and all LiteLLM process-global caches remain untouched.
    """
    loop = asyncio.get_running_loop()
    with _lock:
        owned = _clients.pop(loop, {})
    for client in owned.values():
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - teardown must preserve the original outcome
            pass
