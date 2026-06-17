"""Thin async gateway over LiteLLM.

This is the **only** importer of ``litellm`` (ADR-0004). It exposes ``chat`` /
``stream`` / ``embed`` over domain types from ``app.domain.llm`` and never
leaks a vendor object upward. Notable skeleton behaviours:

* **No network at import time.** ``litellm`` is imported lazily inside the call
  methods, so importing this module (tests, app boot) never reaches out.
* **No-ops gracefully without a key.** When ``OPENROUTER_API_KEY`` is blank
  (the default in ``.env.example``), the gateway raises a typed, mapped error
  on use rather than crashing the process — the skeleton boots LLM-less.

Routing, fallback, cost/limits, and caching are config-driven and elaborated by
CC-9; this skeleton only cuts the seam.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.core.config import Settings
from app.core.errors import DependencyError
from app.domain.llm import ChatMessage, Completion, CompletionChunk, Embedding


class LLMGateway:
    """Async facade over LiteLLM, returning domain types only.

    Construct with the process :class:`Settings`. The model id and embedding
    model id come from config (never hardcoded). When no provider key is
    configured the methods raise :class:`DependencyError`, which the exception
    handler maps to a 503 problem.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def enabled(self) -> bool:
        """Whether a provider key is configured (see ``Settings.llm_enabled``)."""
        return self._settings.llm_enabled

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise DependencyError(
                "LLM provider is not configured (OPENROUTER_API_KEY is blank).",
                code="llm_unconfigured",
            )

    def _credentials(self) -> dict[str, Any]:
        """LiteLLM kwargs carrying the provider key (never logged)."""
        return {"api_key": self._settings.openrouter_api_key}

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
    ) -> Completion:
        """Return a single (non-streamed) completion for ``messages``."""
        self._require_enabled()
        import litellm  # lazy: no network/import cost until first use

        model_id = model or self._settings.llm_model
        response = await litellm.acompletion(
            model=model_id,
            messages=[{"role": m.role.value, "content": m.content} for m in messages],
            stream=False,
            **self._credentials(),
        )
        choice = response.choices[0]
        return Completion(
            content=choice.message.content or "",
            model=model_id,
            finish_reason=getattr(choice, "finish_reason", None),
        )

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
    ) -> AsyncIterator[CompletionChunk]:
        """Yield incremental completion chunks for ``messages``."""
        self._require_enabled()
        import litellm  # lazy

        model_id = model or self._settings.llm_model
        response = await litellm.acompletion(
            model=model_id,
            messages=[{"role": m.role.value, "content": m.content} for m in messages],
            stream=True,
            **self._credentials(),
        )
        index = 0
        async for part in response:
            delta = part.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                yield CompletionChunk(delta=content, index=index)
                index += 1

    async def embed(
        self,
        inputs: Sequence[str],
        *,
        model: str | None = None,
    ) -> list[Embedding]:
        """Return one :class:`Embedding` per input string."""
        self._require_enabled()
        import litellm  # lazy

        model_id = model or self._settings.llm_embedding_model
        response = await litellm.aembedding(
            model=model_id,
            input=list(inputs),
            **self._credentials(),
        )
        return [Embedding(vector=list(item["embedding"]), model=model_id) for item in response.data]
