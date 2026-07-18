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

**Tool-calling (CC-6 #24).** :meth:`stream_tools` streams a tool-aware turn:
incremental answer tokens plus the model's tool-call requests, parsed into
domain :class:`~app.domain.llm.ToolCall` / :class:`~app.domain.llm.StreamEvent`
(never a vendor tool object). The agentic loop runs the tools and calls back for
the next turn. Routing, fallback, cost/limits, and caching remain OUT of this
scope (issue #25 fences) and get their own decision later.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any

from app.core.config import Settings
from app.core.errors import AppError, DependencyError, ValidationError
from app.domain.llm import (
    ChatMessage,
    Completion,
    CompletionChunk,
    Embedding,
    StreamEvent,
    TokenUsage,
    ToolCall,
    ToolSpec,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable


class LlmProviderError(DependencyError):
    """A provider fault, classified for turn-level resilience (ADR-0016 §4, #413).

    The gateway is the ONLY reader of vendor exceptions, so retryability is
    decided here, once, and carried as typed state — the runtime never inspects
    a vendor error. ``retryable`` is True for transient weather (timeout,
    connection fault, 429 rate limit, provider 5xx) where a short backoff + a
    bounded retry is sound; False for faults a retry cannot fix (auth /
    permission / configuration — and anything unexpected, conservatively:
    hammering an unknown failure is worse than failing fast).
    ``retry_after_seconds`` is the provider's ``Retry-After`` hint when one was
    present on a 429 (parsed defensively, capped by the caller) — ``None``
    otherwise. Status/code stay exactly the pre-#413 ``DependencyError``
    surface, so every existing handler and the terminal envelope are unchanged.
    """

    def __init__(
        self,
        detail: str,
        *,
        code: str = "llm_unavailable",
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(detail, code=code)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


def _retry_after_hint(exc: Exception) -> float | None:
    """The 429's ``Retry-After`` seconds, if the vendor response carried one.

    Parsed defensively (the header is attacker-adjacent input): only a plain
    non-negative number is honored; date-form and garbage values are ignored.
    The CAP is applied by the retry loop, not here — the gateway only reports.
    Never touches the exception MESSAGE (which may carry the api_key).
    """
    try:
        # The WHOLE extraction sits under one broad guard: ``response`` /
        # ``headers`` may be hostile property objects, ``get`` may raise, and
        # the value's ``__str__`` may raise — none of that may escape this
        # boundary (a 429 must stay the typed retryable 503, never an opaque
        # 500). Only finite, non-negative numerics are honored.
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers is None:
            return None
        raw = headers.get("retry-after")
        if raw is None:
            return None
        seconds = float(str(raw).strip())
    except Exception:  # noqa: BLE001 — attacker-adjacent input; contain everything
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _cache_family(model: str | None) -> str | None:
    """The provider cache family for a model id — resolved INSIDE the gateway.

    The capability map lives here (ADR-0016 §2.4 / #427 item 14: no provider
    categories leak above ``llm/``): "anthropic" ⇒ Anthropic-style
    ``cache_control`` breakpoints; "openai" ⇒ OpenAI-style implicit prefix
    caching steered by ``prompt_cache_key``; ``None`` ⇒ no cache directives at
    all (AC-3: an unsupported provider behaves exactly as before #411).
    Heuristic over the routed id (which may carry gateway prefixes like
    ``openrouter/``) — conservative: unknown families get nothing.
    """
    if not model:
        return None
    lowered = model.lower()
    if "claude" in lowered or "anthropic/" in lowered:
        return "anthropic"
    if "gpt-" in lowered or "openai/" in lowered:
        return "openai"
    return None


def _apply_cache_directives(
    wire: list[dict[str, Any]], family: str | None
) -> list[dict[str, Any]]:
    """Decorate serialized messages with Anthropic cache breakpoints (#411).

    Applied AFTER :meth:`_to_wire_messages` so the assembler's token-count
    mirror keeps matching the undecorated shape (the ~20 tokens of directive
    framing sit comfortably inside the assembler's 1024-token safety margin).
    Two breakpoints (limit is four): the FIRST message (the stable
    system/tools prefix boundary) and the SECOND-TO-LAST (the moving per-turn
    mark — everything up to and including the previous turn's tool results
    caches; only the newest message is fresh). Only plain-string contents are
    wrapped; tool-call-only messages with empty content are skipped (nothing
    to anchor). Non-Anthropic families return the wire untouched.
    """
    if family != "anthropic" or not wire:
        return wire
    anchors = {0}
    if len(wire) >= 2:
        anchors.add(len(wire) - 2)
    for i in anchors:
        content = wire[i].get("content")
        if isinstance(content, str) and content:
            wire[i] = {
                **wire[i],
                "content": [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
    return wire


def _map_vendor_error(exc: Exception) -> AppError:
    """Map a LiteLLM/provider exception to a typed :class:`AppError`.

    The mapping keys off the LiteLLM exception class (all descend from
    ``litellm.exceptions.APIError``):

    * caller-correctable request faults (bad request, unknown model, oversize
      context, unsupported params) -> :class:`ValidationError` (422);
    * everything else — auth/permission, rate limit, timeout, connection,
      provider 5xx — is a downstream-dependency fault -> :class:`LlmProviderError`
      (a 503 :class:`DependencyError`), classified retryable/terminal for the
      #413 turn-resilience loop (ADR-0016 §4): timeout / connection / 429 /
      provider 5xx are retryable; auth / permission / config are terminal;
      anything unexpected is terminal (conservative).

    Only the vendor exception's **class name** is carried into the mapped
    error's detail — never its message, which LiteLLM is known to populate with
    the ``api_key`` (no secrets logged, backend/AGENTS.md). Call sites raise the
    mapped error with ``from None`` so the key-bearing vendor traceback never
    propagates into logs or to the client (AC-7).
    """
    import litellm.exceptions as le  # lazy: keep litellm import inside llm/

    vendor = type(exc).__name__
    detail = f"The model provider is unavailable ({vendor})."

    # Request-shaped faults the caller could fix -> 422. ContextWindowExceeded
    # and UnsupportedParams subclass BadRequestError, so the base catches them.
    if isinstance(exc, le.BadRequestError | le.NotFoundError):
        return ValidationError(
            f"The model request was rejected ({vendor}).",
            code="llm_bad_request",
        )

    # Transient provider weather — a bounded retry is sound (ADR-0016 §4).
    if isinstance(exc, le.RateLimitError):
        return LlmProviderError(
            detail, retryable=True, retry_after_seconds=_retry_after_hint(exc)
        )
    if isinstance(exc, le.Timeout | le.APIConnectionError):
        return LlmProviderError(detail, retryable=True)
    if isinstance(exc, le.InternalServerError | le.ServiceUnavailableError):
        return LlmProviderError(detail, retryable=True)

    # Auth / permission / anything else API-shaped: a retry cannot fix it.
    if isinstance(exc, le.APIError):
        return LlmProviderError(detail, retryable=False)

    # Anything else from the call path (unexpected) — stay opaque, do not leak,
    # and do not retry what we cannot classify.
    return LlmProviderError(detail, retryable=False)


# --- query-embedding cache (#395) -------------------------------------------
# Search re-embeds the IDENTICAL query text on every facet change (each filter
# is a server param, #118), paying a provider roundtrip for a deterministic
# result. A small process-local LRU+TTL cache absorbs those repeats. Scope is
# deliberately OPT-IN: the caller must supply a ``cache_namespace`` (the
# retrieval chokepoint passes the tenant id), so cardinality never infers
# intent — a single-chunk ingestion batch takes the uncached path because
# ingestion passes no namespace, and entries are never shared across
# namespaces/tenants. Credential overrides also bypass. The key carries a
# SHA-256 digest of the text (query text is sensitive — the audit trail hashes
# it for the same reason), never the raw string. Capacity/TTL come from
# Settings (LLM_EMBED_CACHE_MAX_ENTRIES / LLM_EMBED_CACHE_TTL_SECONDS).
_embed_cache: OrderedDict[tuple[str, str, str, str], tuple[float, list[float]]] = OrderedDict()


def _monotonic() -> float:  # seam for TTL tests
    return time.monotonic()


def clear_embed_cache() -> None:
    """Drop every cached query embedding (test isolation)."""
    _embed_cache.clear()


def _embed_cache_get(
    key: tuple[str, str, str, str], *, ttl_seconds: float
) -> list[float] | None:
    entry = _embed_cache.get(key)
    if entry is None:
        return None
    stored_at, vector = entry
    if _monotonic() - stored_at > ttl_seconds:
        del _embed_cache[key]
        return None
    _embed_cache.move_to_end(key)
    return vector


def _embed_cache_put(
    key: tuple[str, str, str, str], vector: list[float], *, max_entries: int
) -> None:
    _embed_cache[key] = (_monotonic(), list(vector))
    _embed_cache.move_to_end(key)
    while len(_embed_cache) > max_entries:
        _embed_cache.popitem(last=False)


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

    def _credentials(
        self, *, api_key: str | None = None, api_base: str | None = None
    ) -> dict[str, Any]:
        """LiteLLM kwargs carrying the provider key + base (never logged).

        Defaults to the process ``OPENROUTER_API_KEY`` and the provider's native
        route (no ``api_base``). A per-call ``api_key`` / ``api_base`` OVERRIDES
        those — this is how a per-tenant LLM provider routes a completion through
        its own base URL + decrypted key (PR 2a). Both are stateless per call: they
        are never stored on ``self``. ``None`` (the default) ⇒ behaviour is exactly
        as before.
        """
        creds: dict[str, Any] = {
            "api_key": api_key if api_key is not None else self._settings.openrouter_api_key
        }
        if api_base is not None:
            creds["api_base"] = api_base
        return creds

    @staticmethod
    def _to_wire_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
        """Render domain messages as the role/content dicts LiteLLM expects.

        Plain messages render as ``{"role", "content"}``. An assistant turn that
        requested tools also carries an OpenAI-style ``tool_calls`` array; a tool
        turn carries the ``tool_call_id`` (and optional ``name``) tying the result
        back to its call. This is the only place the vendor wire shape is built.
        """
        wire: list[dict[str, Any]] = []
        for m in messages:
            entry: dict[str, Any] = {"role": m.role.value, "content": m.content}
            if m.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in m.tool_calls
                ]
            if m.tool_call_id is not None:
                entry["tool_call_id"] = m.tool_call_id
            if m.name is not None:
                entry["name"] = m.name
            wire.append(entry)
        return wire

    @staticmethod
    def _to_wire_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
        """Render domain tool specs into the OpenAI/LiteLLM ``tools`` array."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        """Return a single (non-streamed) completion for ``messages``.

        ``model`` defaults to ``Settings.llm_model`` when not passed (AC-1). The
        returned :class:`Completion` carries token usage for later cost &
        observability (AC-5). ``api_key`` / ``api_base`` OVERRIDE the process
        defaults when given — the seam a per-tenant LLM provider routes through
        (PR 2a); ``None`` ⇒ the default credentials, unchanged. ``max_tokens``
        bounds the generation when the caller knows the shape of the answer it
        wants (e.g. the short search direct answer, #395); ``None`` ⇒ unbounded,
        unchanged.
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
                **({"max_tokens": max_tokens} if max_tokens is not None else {}),
                **self._credentials(api_key=api_key, api_base=api_base),
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
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> AsyncIterator[CompletionChunk]:
        """Yield incremental completion chunks for ``messages`` (AC-2).

        This is an async generator. Breaking out of the consumer loop (or the
        consumer being cancelled) closes the underlying provider stream in the
        ``finally`` block, so generation stops cleanly and no task or HTTP
        connection is leaked. ``api_key`` / ``api_base`` OVERRIDE the process
        defaults when given (a per-tenant provider route, PR 2a); ``None`` ⇒ the
        default credentials, unchanged.
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
                **self._credentials(api_key=api_key, api_base=api_base),
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

    async def stream_tools(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        model: str | None = None,
        tool_choice: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        cache_key: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream one tool-aware completion turn, yielding :class:`StreamEvent`s.

        The tool-calling counterpart of :meth:`stream` (CC-6 #24). Sends
        ``messages`` with the ``tools`` the model may call; yields incremental
        ``text`` events as the answer streams, accumulates any tool-call deltas
        (providers stream them argument-fragment by fragment), and emits a final
        event carrying ``tool_calls`` (when the model chose to call tools) plus
        the ``finish_reason`` and ``usage``. The caller (the agentic loop) runs
        the requested tools, appends their results, and calls ``stream_tools``
        again for the next turn — so multi-step tool use is a loop over single
        turns, each fully streamed.

        ``tool_choice`` is the standard OpenAI/LiteLLM control (``"auto"`` |
        ``"none"`` | ``"required"``); it is only sent when given. The chat runtime
        passes ``"none"`` to force a final tool-free synthesis once its tool-turn
        budget is spent (issue #148), so the model answers from the gathered tool
        context instead of calling yet another tool.

        Like :meth:`stream`, this is a cancellable async generator: breaking out
        of the consumer closes the provider stream in the ``finally`` block.
        ``api_key`` / ``api_base`` OVERRIDE the process defaults when given (a
        per-tenant provider route, PR 2a); ``None`` ⇒ the default credentials,
        unchanged.
        """
        self._require_enabled()
        import litellm  # lazy

        model_id = model or self._settings.llm_model
        # ``tool_choice`` is optional: only send it when set so the default turn
        # (auto tool selection) keeps the exact wire shape it has today.
        extra: dict[str, Any] = {}
        if tool_choice is not None:
            extra["tool_choice"] = tool_choice
        # Prompt-cache directives (ADR-0016 §2, #411), keyed by the provider
        # family resolved HERE (no provider categories leak above llm/):
        # Anthropic-style breakpoints decorate the serialized messages below;
        # OpenAI-style implicit caching is steered by prompt_cache_key (the
        # session id) via extra_body passthrough. Unknown families — and a
        # disabled kill-switch — send the exact pre-#411 wire (AC-3).
        family = (
            _cache_family(model_id) if self._settings.chat_prompt_cache_enabled else None
        )
        if family == "openai" and cache_key:
            extra["extra_body"] = {"prompt_cache_key": cache_key}
        try:
            response = await litellm.acompletion(
                model=model_id,
                messages=_apply_cache_directives(
                    self._to_wire_messages(messages), family
                ),
                tools=self._to_wire_tools(tools),
                stream=True,
                stream_options={"include_usage": True},
                timeout=self._settings.llm_timeout_seconds,
                **extra,
                **self._credentials(api_key=api_key, api_base=api_base),
            )
        except Exception as exc:  # noqa: BLE001 — mapped to a typed AppError
            raise _map_vendor_error(exc) from None

        # Tool-call fragments arrive across many chunks keyed by ``index``; we
        # accumulate name + the arguments string per index and parse once at end.
        tool_acc: dict[int, _ToolCallAccumulator] = {}
        tool_call_signalled = False
        finish_reason: str | None = None
        usage: TokenUsage | None = None
        try:
            async for part in response:
                usage = _extract_usage(part) if getattr(part, "usage", None) else usage
                choices = getattr(part, "choices", None)
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.delta
                content = getattr(delta, "content", None)
                if content:
                    yield StreamEvent(text=content)
                fragments = getattr(delta, "tool_calls", None) or []
                if fragments and not tool_call_signalled:
                    # The ADR-0016 §6 classification point (#414): the FIRST
                    # tool-call fragment proves the turn tool-calling. One
                    # provider-neutral signal, before accumulation continues —
                    # the runtime flushes buffered narration and closes the
                    # retry window HERE, not at stream end.
                    tool_call_signalled = True
                    yield StreamEvent(tool_call_started=True)
                for frag in fragments:
                    _accumulate_tool_call(tool_acc, frag)
                fr = getattr(choice, "finish_reason", None)
                if fr:
                    finish_reason = fr
        except Exception as exc:  # noqa: BLE001 — mid-stream provider fault
            if isinstance(exc, AppError):
                raise
            raise _map_vendor_error(exc) from None
        finally:
            await _aclose(response)

        tool_calls = tuple(acc.build() for _, acc in sorted(tool_acc.items()))
        yield StreamEvent(
            tool_calls=tool_calls,
            finish_reason=finish_reason or ("tool_calls" if tool_calls else "stop"),
            usage=usage,
        )

    async def embed(
        self,
        inputs: Sequence[str],
        *,
        model: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        cache_namespace: str | None = None,
    ) -> list[Embedding]:
        """Return one :class:`Embedding` per input string (AC-3).

        Inputs are sent in a single batched call; results preserve input order.
        ``model`` defaults to ``Settings.llm_embedding_model``. ``api_key`` /
        ``api_base`` OVERRIDE the process defaults when given (accepted for
        symmetry with the chat methods so a future embedding-routing PR reuses this
        seam; ``None`` ⇒ the configured embedding endpoint, unchanged).
        """
        self._require_enabled()
        import litellm  # lazy

        model_id = model or self._settings.llm_embedding_model
        # OpenRouter embeddings ride its OpenAI-compatible endpoint (#32): send
        # api_base when configured. Chat does not — it uses the native route. A
        # per-call ``api_base`` overrides the configured embedding base.
        effective_api_base = (
            api_base if api_base is not None else self._settings.llm_embedding_api_base
        )

        # #395: serve a repeated single-text query from the process-local cache —
        # OPT-IN only (the caller passes its namespace; retrieval passes the
        # tenant id). No namespace, no caching: ingestion batches — even a
        # singleton final batch — always go to the provider, and entries are
        # never shared across namespaces. Credential overrides also bypass.
        cacheable = (
            cache_namespace is not None
            and len(inputs) == 1
            and api_key is None
            and api_base is None
        )
        cache_key: tuple[str, str, str, str] | None = None
        if cacheable:
            digest = hashlib.sha256(inputs[0].encode("utf-8")).hexdigest()
            cache_key = (cache_namespace or "", model_id, effective_api_base or "", digest)
            cached = _embed_cache_get(
                cache_key, ttl_seconds=self._settings.llm_embed_cache_ttl_seconds
            )
            if cached is not None:
                # Copy on the way out so a caller mutating its vector cannot
                # poison the cache.
                return [Embedding(vector=list(cached), model=model_id)]

        try:
            response = await litellm.aembedding(
                model=model_id,
                input=list(inputs),
                timeout=self._settings.llm_timeout_seconds,
                **self._credentials(api_key=api_key, api_base=effective_api_base or None),
            )
        except Exception as exc:  # noqa: BLE001 — mapped to a typed AppError
            raise _map_vendor_error(exc) from None

        embeddings = [
            Embedding(vector=list(item["embedding"]), model=model_id) for item in response.data
        ]
        if cache_key is not None and len(embeddings) == 1:
            _embed_cache_put(
                cache_key,
                embeddings[0].vector,
                max_entries=self._settings.llm_embed_cache_max_entries,
            )
        return embeddings


class _ToolCallAccumulator:
    """Accumulates a streamed tool call's fragments into one :class:`ToolCall`.

    Providers stream a tool call across chunks: the ``id`` + ``name`` arrive once
    (usually the first fragment), the ``arguments`` JSON string arrives in pieces.
    This stitches them, then parses the arguments at the end (defensively — a
    malformed/empty arguments string yields ``{}`` rather than crashing the loop).
    """

    def __init__(self) -> None:
        self.id: str = ""
        self.name: str = ""
        self._args = ""

    def feed(self, *, call_id: str | None, name: str | None, arguments: str | None) -> None:
        if call_id:
            self.id = call_id
        if name:
            self.name = name
        if arguments:
            self._args += arguments

    def build(self) -> ToolCall:
        try:
            parsed = json.loads(self._args) if self._args.strip() else {}
        except (ValueError, TypeError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        return ToolCall(id=self.id, name=self.name, arguments=parsed)


def _accumulate_tool_call(acc: dict[int, _ToolCallAccumulator], frag: Any) -> None:
    """Fold one streamed tool-call fragment into the per-index accumulator."""
    index = int(getattr(frag, "index", 0) or 0)
    fn = getattr(frag, "function", None)
    acc.setdefault(index, _ToolCallAccumulator()).feed(
        call_id=getattr(frag, "id", None),
        name=getattr(fn, "name", None) if fn is not None else None,
        arguments=getattr(fn, "arguments", None) if fn is not None else None,
    )


def _as_count(value: Any) -> int:
    """Coerce a provider-reported token count to a safe non-negative int.

    Vendor usage objects are untrusted shapes (#419 review): a stray string,
    ``True``/``False`` (bools ARE ints in Python — a classic trap), a float,
    ``None``, or a negative must never crash extraction, inflate a count, or
    reach the ``done`` envelope (whose contract pins ``minimum: 0``). Booleans
    and unparseable values are 0; everything else clamps non-negative — so the
    emitted usage and the persisted row can never disagree about sign.
    """
    if value is None or isinstance(value, bool):
        return 0
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, count)


def _extract_usage(response: Any) -> TokenUsage:
    """Pull token usage off a LiteLLM response, defensively.

    Some providers omit ``usage`` entirely; in that case return a zeroed
    :class:`TokenUsage`. ``total_tokens`` falls back to the sum when absent.
    Every field passes through :func:`_as_count`, so malformed vendor values
    (strings, bools, negatives) degrade to ``0`` rather than raising or
    breaking the envelope's non-negative contract.

    Cache accounting (#409, ADR-0016 §2.6) is read from BOTH normalized shapes —
    OpenAI-style ``prompt_tokens_details.cached_tokens`` and Anthropic-style
    ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` — whichever the
    provider populated (LiteLLM passes each through as-is). A zero/absent
    ``cached_tokens`` falls back to the Anthropic-style read field; absent/None
    fields stay ``0``: a provider with no cache detail never breaks extraction.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    prompt = _as_count(getattr(usage, "prompt_tokens", 0))
    completion = _as_count(getattr(usage, "completion_tokens", 0))
    total = _as_count(getattr(usage, "total_tokens", 0)) or (prompt + completion)
    details = getattr(usage, "prompt_tokens_details", None)
    cached = _as_count(getattr(details, "cached_tokens", 0)) if details is not None else 0
    if not cached:
        cached = _as_count(getattr(usage, "cache_read_input_tokens", 0))
    cache_write = _as_count(getattr(usage, "cache_creation_input_tokens", 0))
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cached_prompt_tokens=cached,
        cache_write_tokens=cache_write,
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


async def aclose_litellm_clients() -> None:
    """Close LiteLLM's process-global async HTTP clients (#94).

    LiteLLM keeps reusable ``httpx.AsyncClient`` connections alive between calls:
    a ``module_level_aclient`` handler plus a per-model cache
    (``in_memory_llm_clients_cache``). A real call (the key-gated live smoke)
    opens sockets on those clients that are otherwise only released on late GC —
    which, under ``filterwarnings = error``, surfaces as a
    ``ResourceWarning: unclosed socket`` mis-attributed to whatever test runs
    next (see issue #94). Closing them here releases every upstream socket
    deterministically.

    Confined to ``app/llm/`` because it is the only importer of LiteLLM
    (ADR-0004). Best-effort and idempotent: a missing attribute or a failing
    close is swallowed so teardown never masks the test's own result. Safe to
    call even when no live call was made (closing an unused client is a no-op).
    """
    import litellm  # lazy: keep the litellm import inside app/llm/

    async def _close_handler(handler: Any) -> None:
        """Close one client/handler via whatever async-close hook it exposes."""
        for attr in ("close", "aclose"):
            hook: Any = getattr(handler, attr, None)
            if callable(hook):
                try:
                    result = hook()
                    if result is not None:
                        await result
                except Exception:  # noqa: BLE001 — teardown is best-effort
                    pass
                return
        # Some handlers wrap the httpx client on a ``.client`` attribute.
        inner: Any = getattr(handler, "client", None)
        inner_aclose: Any = getattr(inner, "aclose", None) if inner is not None else None
        if callable(inner_aclose):
            try:
                await inner_aclose()
            except Exception:  # noqa: BLE001 — teardown is best-effort
                pass

    # The module-level shared async handler.
    handler = getattr(litellm, "module_level_aclient", None)
    if handler is not None:
        await _close_handler(handler)

    # The per-model client cache: close each cached handler, then clear it so a
    # later call rebuilds fresh clients rather than reusing closed ones.
    cache = getattr(litellm, "in_memory_llm_clients_cache", None)
    cache_dict: Any = getattr(cache, "cache_dict", None) if cache is not None else None
    if isinstance(cache_dict, dict):
        for cached in list(cache_dict.values()):
            await _close_handler(cached)
        try:
            cache_dict.clear()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass
