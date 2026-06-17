"""Thin async gateway over LiteLLM.

This is the **only** importer of ``litellm`` (ADR-0004). It exposes ``chat`` /
``stream`` / ``embed`` over domain types from ``app.domain.llm`` and never
leaks a vendor object upward. Notable behaviours:

* **No network at import time.** ``litellm`` is imported lazily inside the call
  methods, so importing this module (tests, app boot) never reaches out.
* **No-ops gracefully without a key.** When ``OPENROUTER_API_KEY`` is blank
  (the default in ``.env.example``), the gateway raises a typed, mapped error
  on use rather than crashing the process — the backend boots LLM-less.
* **Vendor errors never escape.** Every LiteLLM exception is mapped to a typed
  :class:`AppError` (``DependencyError`` / ``ValidationError``); the original
  vendor exception is chained for logs but never reaches the caller (AC-7).
* **Config-driven.** Model ids, the per-request timeout, and the provider key
  come from :class:`Settings` only — switching ``LLM_MODEL`` or the provider is
  a config change, no code change (AC-4).

Routing, fallback, cost/limits, caching, and tool-calling are deliberately OUT
of this scope (issue #25 fences) and get their own decision later.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any

from app.core.config import Settings
from app.core.errors import AppError, DependencyError, ValidationError
from app.domain.llm import (
    ChatMessage,
    Completion,
    CompletionChunk,
    Embedding,
    TokenUsage,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable


def _map_vendor_error(exc: Exception) -> AppError:
    """Map a LiteLLM/provider exception to a typed :class:`AppError`.

    The mapping keys off the LiteLLM exception class (all descend from
    ``litellm.exceptions.APIError``):

    * caller-correctable request faults (bad request, unknown model, oversize
      context, unsupported params) -> :class:`ValidationError` (422);
    * everything else — auth/permission, rate limit, timeout, connection,
      provider 5xx — is a downstream-dependency fault -> :class:`DependencyError`
      (503).

    Only the vendor exception's **class name** is carried into the mapped
    error's detail — never its message, which LiteLLM is known to populate with
    the ``api_key`` (no secrets logged, backend/AGENTS.md). Call sites raise the
    mapped error with ``from None`` so the key-bearing vendor traceback never
    propagates into logs or to the client (AC-7).
    """
    import litellm.exceptions as le  # lazy: keep litellm import inside llm/

    vendor = type(exc).__name__

    # Request-shaped faults the caller could fix -> 422. ContextWindowExceeded
    # and UnsupportedParams subclass BadRequestError, so the base catches them.
    if isinstance(exc, le.BadRequestError | le.NotFoundError):
        return ValidationError(
            f"The model request was rejected ({vendor}).",
            code="llm_bad_request",
        )

    # Auth/permission, rate-limit, timeout, connection, and provider 5xx are all
    # "the dependency is unavailable / not usable right now" -> 503.
    if isinstance(exc, le.APIError):
        return DependencyError(
            f"The model provider is unavailable ({vendor}).",
            code="llm_unavailable",
        )

    # Anything else from the call path (unexpected) — stay opaque, do not leak.
    return DependencyError(
        f"The model provider is unavailable ({vendor}).",
        code="llm_unavailable",
    )


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

    @staticmethod
    def _to_wire_messages(messages: Sequence[ChatMessage]) -> list[dict[str, str]]:
        """Render domain messages as the role/content dicts LiteLLM expects."""
        return [{"role": m.role.value, "content": m.content} for m in messages]

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
    ) -> Completion:
        """Return a single (non-streamed) completion for ``messages``.

        ``model`` defaults to ``Settings.llm_model`` when not passed (AC-1). The
        returned :class:`Completion` carries token usage for later cost &
        observability (AC-5).
        """
        self._require_enabled()
        import litellm  # lazy: no network/import cost until first use

        model_id = model or self._settings.llm_model
        try:
            response = await litellm.acompletion(
                model=model_id,
                messages=self._to_wire_messages(messages),
                stream=False,
                timeout=self._settings.llm_timeout_seconds,
                **self._credentials(),
            )
        except Exception as exc:  # noqa: BLE001 — mapped to a typed AppError
            raise _map_vendor_error(exc) from None

        choice = response.choices[0]
        return Completion(
            content=choice.message.content or "",
            model=model_id,
            finish_reason=getattr(choice, "finish_reason", None),
            usage=_extract_usage(response),
        )

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
    ) -> AsyncIterator[CompletionChunk]:
        """Yield incremental completion chunks for ``messages`` (AC-2).

        This is an async generator. Breaking out of the consumer loop (or the
        consumer being cancelled) closes the underlying provider stream in the
        ``finally`` block, so generation stops cleanly and no task or HTTP
        connection is leaked.
        """
        self._require_enabled()
        import litellm  # lazy

        model_id = model or self._settings.llm_model
        try:
            response = await litellm.acompletion(
                model=model_id,
                messages=self._to_wire_messages(messages),
                stream=True,
                timeout=self._settings.llm_timeout_seconds,
                **self._credentials(),
            )
        except Exception as exc:  # noqa: BLE001 — mapped to a typed AppError
            raise _map_vendor_error(exc) from None

        index = 0
        try:
            async for part in response:
                delta = part.choices[0].delta
                content = getattr(delta, "content", None)
                if content:
                    yield CompletionChunk(delta=content, index=index)
                    index += 1
        except Exception as exc:  # noqa: BLE001 — mid-stream provider fault
            if isinstance(exc, AppError):
                raise
            raise _map_vendor_error(exc) from None
        finally:
            # Cancellable: close the provider stream on break / cancel / error so
            # the upstream HTTP connection is released and no task lingers.
            await _aclose(response)

    async def embed(
        self,
        inputs: Sequence[str],
        *,
        model: str | None = None,
    ) -> list[Embedding]:
        """Return one :class:`Embedding` per input string (AC-3).

        Inputs are sent in a single batched call; results preserve input order.
        ``model`` defaults to ``Settings.llm_embedding_model``.
        """
        self._require_enabled()
        import litellm  # lazy

        model_id = model or self._settings.llm_embedding_model
        try:
            response = await litellm.aembedding(
                model=model_id,
                input=list(inputs),
                timeout=self._settings.llm_timeout_seconds,
                **self._credentials(),
            )
        except Exception as exc:  # noqa: BLE001 — mapped to a typed AppError
            raise _map_vendor_error(exc) from None

        return [Embedding(vector=list(item["embedding"]), model=model_id) for item in response.data]


def _extract_usage(response: Any) -> TokenUsage:
    """Pull token usage off a LiteLLM response, defensively.

    Some providers omit ``usage`` entirely; in that case return a zeroed
    :class:`TokenUsage`. ``total_tokens`` falls back to the sum when absent.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    total = int(getattr(usage, "total_tokens", 0) or 0) or (prompt + completion)
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
    )


async def _aclose(stream: Any) -> None:
    """Close a LiteLLM async stream if it exposes a close hook.

    LiteLLM's streaming wrapper may expose ``aclose`` (async) or ``close``
    (sync); either releases the upstream connection. Best-effort: a missing or
    failing close must not mask the original control flow.
    """
    aclose: Any = getattr(stream, "aclose", None)
    if callable(aclose):
        try:
            result: Awaitable[Any] | None = aclose()
            if result is not None:
                await result
        except Exception:  # noqa: BLE001 — teardown is best-effort
            return
        return
    close: Any = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            return
