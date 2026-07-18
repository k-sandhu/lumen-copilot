"""Unit tests for the LLM gateway (``app.llm.gateway``).

LiteLLM is monkeypatched throughout (no network) to assert the gateway's
contract from issue #25:

* AC-1 chat -> ``Completion`` domain object; model id defaults from config.
* AC-2 stream yields ``CompletionChunk``s and stops cleanly on consumer break.
* AC-3 embed returns one ``Embedding`` per input, dimension from the model.
* AC-4 model ids / timeout / creds come from config only (no literals).
* AC-5 ``Completion`` carries token usage.
* AC-6 blank key -> ``DependencyError`` (-> 503), never a hang or raw traceback.
* AC-7 provider error/timeout/unknown-model -> typed ``AppError``; vendor
  exception never escapes.
* AC-8 callers receive only domain types; ``litellm`` is imported only under
  ``app/llm/``.

A key-gated live smoke (``OPENROUTER_API_KEY``) does one tiny real call; it is
skipped when no key is present (the default in dev).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path
from typing import Any

import litellm
import litellm.exceptions as le
import pytest

from app.core.config import Settings
from app.core.errors import AppError, DependencyError, ValidationError
from app.domain.llm import (
    ChatMessage,
    Completion,
    CompletionChunk,
    Embedding,
    Role,
    TokenUsage,
)
from app.llm.gateway import LLMGateway, aclose_litellm_clients, clear_embed_cache


@pytest.fixture(autouse=True)
def _fresh_embed_cache() -> Any:
    """Isolate the module-level query-embedding cache (#395) between tests."""
    clear_embed_cache()
    yield
    clear_embed_cache()

# --- Test settings ---------------------------------------------------------

_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:5432/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "CELERY_BROKER_URL": "redis://localhost:6379/1",
    "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_ACCESS_KEY": "test",
    "S3_SECRET_KEY": "test_secret",
    "S3_BUCKET": "test-bucket",
}


def _settings(**overrides: str) -> Settings:
    """Build a Settings instance from explicit values (no .env / environment)."""
    values: dict[str, Any] = {
        **_BASE_ENV,
        "OPENROUTER_API_KEY": "sk-test-key",
        "LLM_MODEL": "openrouter/openai/gpt-4o-mini",
        "LLM_EMBEDDING_MODEL": "openai/baai/bge-m3",
        "LLM_EMBEDDING_API_BASE": "https://openrouter.ai/api/v1",
        "LLM_TIMEOUT_SECONDS": "60",
        **overrides,
    }
    return Settings(**values)  # type: ignore[arg-type]


# --- Fakes mirroring the LiteLLM response shape the gateway reads ----------


class _Msg:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str | None, finish_reason: str | None) -> None:
        self.message = _Msg(content)
        self.finish_reason = finish_reason


class _Usage:
    def __init__(self, prompt: int, completion: int, total: int | None) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total


class _Response:
    """A vendor-shaped completion response (NOT a domain type)."""

    def __init__(
        self,
        content: str,
        *,
        finish_reason: str | None = "stop",
        usage: _Usage | None = None,
    ) -> None:
        self.choices = [_Choice(content, finish_reason)]
        self.usage = usage


class _Delta:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _StreamChoice:
    def __init__(self, content: str | None) -> None:
        self.delta = _Delta(content)


class _StreamPart:
    def __init__(self, content: str | None) -> None:
        self.choices = [_StreamChoice(content)]


class _FakeStream:
    """Async-iterable vendor stream that records consumption and close."""

    def __init__(self, contents: list[str | None]) -> None:
        self._contents = contents
        self.closed = False
        self.consumed: list[str | None] = []

    def __aiter__(self) -> _FakeStream:
        self._it = iter(self._contents)
        return self

    async def __anext__(self) -> _StreamPart:
        try:
            item = next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None
        self.consumed.append(item)
        return _StreamPart(item)

    async def aclose(self) -> None:
        self.closed = True


class _EmbeddingResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [{"embedding": v} for v in vectors]


# --- AC-1 / AC-5: chat returns a Completion with usage; model from config --


async def test_chat_returns_completion_domain_object(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response("hello there", usage=_Usage(3, 5, 8))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    result = await gw.chat([ChatMessage(role=Role.USER, content="hi")])

    assert isinstance(result, Completion)
    assert result.content == "hello there"
    assert result.finish_reason == "stop"
    # AC-1: model id defaulted from config, not a literal in the gateway.
    assert result.model == "openrouter/openai/gpt-4o-mini"
    assert captured["model"] == "openrouter/openai/gpt-4o-mini"
    # AC-4: timeout + creds sourced from config.
    assert captured["timeout"] == 60.0
    assert captured["api_key"] == "sk-test-key"
    assert captured["stream"] is False
    # Messages rendered as role/content dicts.
    assert captured["messages"] == [{"role": "user", "content": "hi"}]
    # AC-5: token usage carried through.
    assert result.usage == TokenUsage(prompt_tokens=3, completion_tokens=5, total_tokens=8)


async def test_chat_uses_explicit_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response("ok")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    result = await gw.chat(
        [ChatMessage(role=Role.USER, content="hi")],
        model="openrouter/anthropic/claude-3.5-sonnet",
    )

    assert captured["model"] == "openrouter/anthropic/claude-3.5-sonnet"
    assert result.model == "openrouter/anthropic/claude-3.5-sonnet"


async def test_chat_api_key_and_base_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # PR 2a: an explicit api_key/api_base OVERRIDE the process defaults in the
    # litellm call (the seam a per-tenant provider routes through). They are never
    # stored on the gateway — passed per call.
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response("ok")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    await gw.chat(
        [ChatMessage(role=Role.USER, content="hi")],
        model="openai/gpt-4o",
        api_key="sk-provider-key",
        api_base="https://provider.example.com/v1",
    )

    assert captured["api_key"] == "sk-provider-key"  # overrides sk-test-key
    assert captured["api_base"] == "https://provider.example.com/v1"
    assert captured["model"] == "openai/gpt-4o"


async def test_chat_without_override_uses_default_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PR 2a: omitting api_key/api_base is exactly today's behaviour — the default
    # OPENROUTER_API_KEY, and no api_base on the chat (native) route.
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response("ok")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    await gw.chat([ChatMessage(role=Role.USER, content="hi")])

    assert captured["api_key"] == "sk-test-key"
    assert "api_base" not in captured


async def test_stream_tools_api_key_and_base_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # The chat runtime routes provider completions through stream_tools; assert the
    # override reaches the litellm call there too.
    from app.domain.llm import ToolSpec

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeStream:
        captured.update(kwargs)
        return _FakeStream([None])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    spec = ToolSpec(name="search", description="d", parameters={"type": "object"})
    events = [
        ev
        async for ev in gw.stream_tools(
            [ChatMessage(role=Role.USER, content="hi")],
            tools=[spec],
            model="openai/gpt-4o",
            api_key="sk-provider-key",
            api_base="https://provider.example.com/v1",
        )
    ]

    assert events  # a terminal event was produced
    assert captured["model"] == "openai/gpt-4o"
    assert captured["api_key"] == "sk-provider-key"
    assert captured["api_base"] == "https://provider.example.com/v1"


async def test_chat_usage_absent_yields_zeroed_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**kwargs: Any) -> _Response:
        return _Response("no usage", usage=None)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    result = await gw.chat([ChatMessage(role=Role.USER, content="hi")])
    assert result.usage == TokenUsage(0, 0, 0)


async def test_chat_usage_total_falls_back_to_sum(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**kwargs: Any) -> _Response:
        return _Response("x", usage=_Usage(4, 6, None))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    result = await gw.chat([ChatMessage(role=Role.USER, content="hi")])
    assert result.usage.total_tokens == 10


class _UsageDetails:
    """OpenAI-style ``prompt_tokens_details`` (the LiteLLM-normalized shape)."""

    def __init__(self, cached_tokens: int) -> None:
        self.cached_tokens = cached_tokens


class _CacheUsage(_Usage):
    """A vendor usage object carrying the provider cache-accounting fields (#409).

    OpenAI-style reports cached reads under ``prompt_tokens_details.cached_tokens``;
    Anthropic-style reports ``cache_read_input_tokens`` / ``cache_creation_input_tokens``
    (both as LiteLLM normalizes them). ``None`` models a provider omitting the field.
    """

    def __init__(
        self,
        prompt: int,
        completion: int,
        total: int | None,
        *,
        cached_details: int | None = None,
        cache_read: int | None = None,
        cache_creation: int | None = None,
    ) -> None:
        super().__init__(prompt, completion, total)
        self.prompt_tokens_details = (
            _UsageDetails(cached_details) if cached_details is not None else None
        )
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_creation


async def test_chat_usage_extracts_openai_style_cached_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#409 — cached prompt tokens ride ``prompt_tokens_details.cached_tokens``."""

    async def fake_acompletion(**kwargs: Any) -> _Response:
        return _Response("x", usage=_CacheUsage(100, 5, 105, cached_details=64))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    result = await gw.chat([ChatMessage(role=Role.USER, content="hi")])
    assert result.usage.cached_prompt_tokens == 64
    assert result.usage.cache_write_tokens == 0


async def test_chat_usage_extracts_anthropic_style_cache_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#409 — Anthropic-style cache read/creation fields map to the domain usage."""

    async def fake_acompletion(**kwargs: Any) -> _Response:
        return _Response("x", usage=_CacheUsage(100, 5, 105, cache_read=90, cache_creation=10))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    result = await gw.chat([ChatMessage(role=Role.USER, content="hi")])
    assert result.usage.cached_prompt_tokens == 90
    assert result.usage.cache_write_tokens == 10


async def test_chat_usage_cache_fields_default_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """#409 negative — providers reporting no cache detail yield zeros, never a crash."""

    async def fake_acompletion(**kwargs: Any) -> _Response:
        return _Response("x", usage=_Usage(3, 5, 8))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    result = await gw.chat([ChatMessage(role=Role.USER, content="hi")])
    assert result.usage.cached_prompt_tokens == 0
    assert result.usage.cache_write_tokens == 0


async def test_chat_usage_cached_zero_falls_back_to_anthropic_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#419 review — a present-but-zero ``cached_tokens`` falls back to the
    Anthropic-style ``cache_read_input_tokens`` rather than masking it."""

    async def fake_acompletion(**kwargs: Any) -> _Response:
        return _Response(
            "x", usage=_CacheUsage(100, 5, 105, cached_details=0, cache_read=77)
        )

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    result = await gw.chat([ChatMessage(role=Role.USER, content="hi")])
    assert result.usage.cached_prompt_tokens == 77


class _HostileUsage:
    """A vendor usage object with malformed fields (#419 review — untrusted shape).

    A stray string, a bool (``True`` IS an int in Python — the classic trap), a
    float, and a negative must all degrade to a safe non-negative int, never
    raise, and never reach the ``done`` envelope's ``minimum: 0`` contract.
    """

    def __init__(self) -> None:
        self.prompt_tokens = "not-a-number"
        self.completion_tokens = True  # noqa: FBT003 — deliberately hostile
        self.total_tokens = -50
        self.prompt_tokens_details = None
        self.cache_read_input_tokens = 3.9
        self.cache_creation_input_tokens = -1


async def test_chat_usage_coerces_malformed_vendor_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#419 review — malformed/bool/negative usage values coerce safely, no crash."""

    async def fake_acompletion(**kwargs: Any) -> _Response:
        return _Response("x", usage=_HostileUsage())

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    usage = (await gw.chat([ChatMessage(role=Role.USER, content="hi")])).usage
    assert usage.prompt_tokens == 0  # unparseable string → 0
    assert usage.completion_tokens == 0  # bool is NOT a token count
    # total was negative → clamped 0, then the sum fallback (0+0) also 0.
    assert usage.total_tokens == 0
    assert usage.cached_prompt_tokens == 3  # float truncates, non-negative
    assert usage.cache_write_tokens == 0  # negative → clamped 0
    # Every field satisfies the envelope's minimum:0 contract.
    assert all(
        v >= 0
        for v in (
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
            usage.cached_prompt_tokens,
            usage.cache_write_tokens,
        )
    )


# --- AC-2: streaming yields chunks; consumer break stops generation cleanly -


async def test_stream_yields_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeStream(["Hel", "lo", None, "!"])

    async def fake_acompletion(**kwargs: Any) -> _FakeStream:
        assert kwargs["stream"] is True
        return fake

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    chunks: list[CompletionChunk] = []
    async for chunk in gw.stream([ChatMessage(role=Role.USER, content="hi")]):
        assert isinstance(chunk, CompletionChunk)
        chunks.append(chunk)

    # Empty-content deltas are skipped; indices are contiguous.
    assert [c.delta for c in chunks] == ["Hel", "lo", "!"]
    assert [c.index for c in chunks] == [0, 1, 2]
    # Stream closed at the end of consumption.
    assert fake.closed is True


async def test_stream_break_closes_provider_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-2: breaking out of the consumer loop stops generation cleanly.

    A correct consumer wraps the async generator in ``aclosing`` (or relies on
    the event loop's async-generator finalizer); either way ``aclose`` is
    invoked on early exit, which fires the gateway's ``finally`` and closes the
    underlying provider stream — no leaked task or HTTP connection.
    """
    fake = _FakeStream(["a", "b", "c", "d"])

    async def fake_acompletion(**kwargs: Any) -> _FakeStream:
        return fake

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    seen: list[str] = []
    async with aclosing(gw.stream([ChatMessage(role=Role.USER, content="hi")])) as stream:
        async for chunk in stream:
            seen.append(chunk.delta)
            if len(seen) == 2:
                break  # cancel mid-stream

    assert seen == ["a", "b"]
    # The finally block closed the underlying provider stream on early exit.
    assert fake.closed is True
    # The provider was not drained past the break point.
    assert fake.consumed == ["a", "b"]


# --- AC-3: embed returns one Embedding per input; dimension from the model --


async def test_embed_returns_one_embedding_per_input(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> _EmbeddingResponse:
        captured.update(kwargs)
        return _EmbeddingResponse([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
    gw = LLMGateway(_settings())

    result = await gw.embed(["alpha", "beta"])

    assert len(result) == 2
    assert all(isinstance(e, Embedding) for e in result)
    # Dimension matches the (fake) model output for each input.
    assert [len(e.vector) for e in result] == [3, 3]
    assert result[0].vector == [0.1, 0.2, 0.3]
    # AC-3 + AC-4: batched single call, model + creds from config.
    assert captured["input"] == ["alpha", "beta"]
    assert captured["model"] == "openai/baai/bge-m3"
    assert captured["api_key"] == "sk-test-key"
    assert captured["timeout"] == 60.0
    # #32: embeddings ride OpenRouter's OpenAI-compatible endpoint via api_base.
    assert captured["api_base"] == "https://openrouter.ai/api/v1"
    assert result[0].model == "openai/baai/bge-m3"


async def test_embed_uses_explicit_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> _EmbeddingResponse:
        captured.update(kwargs)
        return _EmbeddingResponse([[1.0]])

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
    gw = LLMGateway(_settings())

    await gw.embed(["x"], model="openrouter/voyage/voyage-3")
    assert captured["model"] == "openrouter/voyage/voyage-3"


async def test_embed_omits_api_base_when_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """No api_base override when LLM_EMBEDDING_API_BASE is blank (#32).

    Blanking the base lets a directly-routed embedding model (e.g. a native
    LiteLLM provider) be used without the OpenRouter OpenAI-compatible shim.
    """
    captured: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> _EmbeddingResponse:
        captured.update(kwargs)
        return _EmbeddingResponse([[1.0]])

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
    gw = LLMGateway(_settings(LLM_EMBEDDING_API_BASE=""))

    await gw.embed(["x"])
    assert "api_base" not in captured


# --- #395: query-embedding cache (single-text, default-credential calls) ----


async def test_embed_caches_repeated_single_text_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SAME query text embeds once; the repeat is served from cache (#395).

    Search re-embeds the identical query on every facet change (every filter is
    a server param, #118) — the second call must not pay the provider roundtrip.
    """
    calls = 0

    async def fake_aembedding(**kwargs: Any) -> _EmbeddingResponse:
        nonlocal calls
        calls += 1
        return _EmbeddingResponse([[0.1, 0.2]])

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
    gw = LLMGateway(_settings())

    first = await gw.embed(["connection pool"], cache_namespace="tenant-a")
    second = await gw.embed(["connection pool"], cache_namespace="tenant-a")
    assert calls == 1
    assert second[0].vector == first[0].vector

    # A caller mutating its returned vector must not poison the cache.
    second[0].vector.append(9.9)
    third = await gw.embed(["connection pool"], cache_namespace="tenant-a")
    assert third[0].vector == [0.1, 0.2]


async def test_embed_cache_scopes_by_text_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def fake_aembedding(**kwargs: Any) -> _EmbeddingResponse:
        nonlocal calls
        calls += 1
        return _EmbeddingResponse([[float(calls)]])

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
    gw = LLMGateway(_settings())

    await gw.embed(["alpha"], cache_namespace="t1")
    await gw.embed(["beta"], cache_namespace="t1")  # different text → miss
    await gw.embed(  # different model → miss
        ["alpha"], model="openrouter/voyage/voyage-3", cache_namespace="t1"
    )
    # Different NAMESPACE → miss: one tenant's entries are never observable from
    # another (no cross-principal latency side-channel).
    await gw.embed(["alpha"], cache_namespace="t2")
    assert calls == 4


async def test_embed_cache_skips_batches_and_credential_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bulk (ingestion) embeds and per-tenant credential overrides are never cached."""
    calls = 0

    async def fake_aembedding(**kwargs: Any) -> _EmbeddingResponse:
        nonlocal calls
        calls += 1
        n = len(kwargs["input"])
        return _EmbeddingResponse([[1.0]] * n)

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
    gw = LLMGateway(_settings())

    await gw.embed(["a", "b"], cache_namespace="t1")
    await gw.embed(["a", "b"], cache_namespace="t1")  # batches bypass the cache
    assert calls == 2

    # Overrides bypass even with a namespace.
    await gw.embed(["a"], api_key="sk-tenant-override", cache_namespace="t1")
    await gw.embed(["a"], api_key="sk-tenant-override", cache_namespace="t1")
    assert calls == 4

    # The ingestion regression: a SINGLETON batch with NO namespace (ingestion
    # never passes one) always goes to the provider — cardinality is not intent.
    await gw.embed(["single chunk document"])
    await gw.embed(["single chunk document"])
    assert calls == 6


async def test_embed_cache_entries_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def fake_aembedding(**kwargs: Any) -> _EmbeddingResponse:
        nonlocal calls
        calls += 1
        return _EmbeddingResponse([[1.0]])

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
    gw = LLMGateway(_settings())

    fake_now = [1000.0]
    monkeypatch.setattr("app.llm.gateway._monotonic", lambda: fake_now[0])

    await gw.embed(["alpha"], cache_namespace="t1")
    fake_now[0] += 10.0
    await gw.embed(["alpha"], cache_namespace="t1")  # still fresh → cached
    assert calls == 1

    fake_now[0] += 20 * 60.0  # past the (default 900s) TTL
    await gw.embed(["alpha"], cache_namespace="t1")
    assert calls == 2


async def test_embed_cache_limits_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Capacity/TTL are config knobs (backend/AGENTS.md: limits from config)."""
    calls = 0

    async def fake_aembedding(**kwargs: Any) -> _EmbeddingResponse:
        nonlocal calls
        calls += 1
        return _EmbeddingResponse([[1.0]])

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
    gw = LLMGateway(_settings(LLM_EMBED_CACHE_TTL_SECONDS="1.5"))

    fake_now = [0.0]
    monkeypatch.setattr("app.llm.gateway._monotonic", lambda: fake_now[0])

    await gw.embed(["alpha"], cache_namespace="t1")
    fake_now[0] += 1.0
    await gw.embed(["alpha"], cache_namespace="t1")  # inside the configured TTL
    assert calls == 1
    fake_now[0] += 1.0  # 2.0 total — past the 1.5s configured TTL
    await gw.embed(["alpha"], cache_namespace="t1")
    assert calls == 2


# --- #395: bounded completions ----------------------------------------------


async def test_chat_passes_max_tokens_when_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _Response("ok")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    await gw.chat([ChatMessage(role=Role.USER, content="hi")], max_tokens=300)
    assert captured["max_tokens"] == 300


async def test_chat_omits_max_tokens_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _Response("ok")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    await gw.chat([ChatMessage(role=Role.USER, content="hi")])
    assert "max_tokens" not in captured


# --- AC-6: blank key -> DependencyError, no network, no hang ----------------


async def test_blank_key_chat_raises_dependency_error() -> None:
    gw = LLMGateway(_settings(OPENROUTER_API_KEY=""))
    assert gw.enabled is False
    with pytest.raises(DependencyError) as exc_info:
        await gw.chat([ChatMessage(role=Role.USER, content="hi")])
    assert exc_info.value.status == 503
    assert exc_info.value.code == "llm_unconfigured"


async def test_blank_key_embed_raises_dependency_error() -> None:
    gw = LLMGateway(_settings(OPENROUTER_API_KEY=""))
    with pytest.raises(DependencyError):
        await gw.embed(["hi"])


async def test_blank_key_stream_raises_dependency_error() -> None:
    gw = LLMGateway(_settings(OPENROUTER_API_KEY=""))
    with pytest.raises(DependencyError):
        async for _ in gw.stream([ChatMessage(role=Role.USER, content="hi")]):
            pass


# --- AC-7: provider faults map to typed AppErrors; vendor error never leaks -


@pytest.mark.parametrize(
    ("vendor_exc", "expected"),
    [
        (le.AuthenticationError("bad key", "openrouter", "gpt"), DependencyError),
        (le.RateLimitError("slow down", "openrouter", "gpt"), DependencyError),
        (le.Timeout("timed out", "gpt", "openrouter"), DependencyError),
        (
            le.APIConnectionError(message="conn", llm_provider="openrouter", model="gpt"),
            DependencyError,
        ),
        (le.ServiceUnavailableError("503", "openrouter", "gpt"), DependencyError),
        (le.InternalServerError("500", "openrouter", "gpt"), DependencyError),
        (le.BadRequestError("bad", "gpt", "openrouter"), ValidationError),
        (le.NotFoundError("unknown model", "gpt", "openrouter"), ValidationError),
        (le.ContextWindowExceededError("too big", "gpt", "openrouter"), ValidationError),
    ],
)
async def test_chat_maps_vendor_errors(
    monkeypatch: pytest.MonkeyPatch,
    vendor_exc: Exception,
    expected: type[AppError],
) -> None:
    async def fake_acompletion(**kwargs: Any) -> _Response:
        raise vendor_exc

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    with pytest.raises(AppError) as exc_info:
        await gw.chat([ChatMessage(role=Role.USER, content="hi")])

    # Mapped to the expected typed error...
    assert isinstance(exc_info.value, expected)
    # ...and no litellm type leaks across the boundary as the raised exception.
    assert not isinstance(exc_info.value, le.APIError)
    # The key-bearing vendor exception is NOT chained (`from None`), so the raw
    # vendor message can't reach logs/tracebacks (no secrets logged, AC-7).
    assert exc_info.value.__cause__ is None
    # Only the vendor *class name* is surfaced — never the vendor message.
    detail = exc_info.value.detail or ""
    assert type(vendor_exc).__name__ in detail
    assert str(vendor_exc) not in detail


async def test_embed_maps_vendor_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_aembedding(**kwargs: Any) -> _EmbeddingResponse:
        raise le.RateLimitError("slow down", "openrouter", "embed")

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
    gw = LLMGateway(_settings())

    with pytest.raises(DependencyError):
        await gw.embed(["x"])


async def test_vendor_error_never_leaks_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-7 + no-secrets: the api_key never appears in the mapped error or chain.

    LiteLLM is known to embed the ``api_key`` in its exception message. The
    mapped :class:`AppError` must carry none of it — not in its message, and not
    via a chained ``__cause__`` that a logger could render.
    """
    secret = "sk-test-key"  # matches _settings() default OPENROUTER_API_KEY

    async def fake_acompletion(**kwargs: Any) -> _Response:
        raise le.AuthenticationError(f"invalid api_key: {secret}", "openrouter", "gpt")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    with pytest.raises(AppError) as exc_info:
        await gw.chat([ChatMessage(role=Role.USER, content="hi")])

    err = exc_info.value
    assert secret not in str(err)
    assert secret not in (err.detail or "")
    # No explicit cause, and implicit context is suppressed (`from None`), so a
    # printed traceback won't render the key-bearing vendor message.
    assert err.__cause__ is None
    assert err.__suppress_context__ is True


async def test_stream_setup_error_maps_to_app_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**kwargs: Any) -> _FakeStream:
        raise le.AuthenticationError("bad key", "openrouter", "gpt")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    with pytest.raises(DependencyError):
        async for _ in gw.stream([ChatMessage(role=Role.USER, content="hi")]):
            pass


async def test_stream_midstream_error_maps_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FailingStream(_FakeStream):
        async def __anext__(self) -> _StreamPart:
            if not self.closed and self._contents:
                first = self._contents.pop(0)
                if first is not None:
                    return _StreamPart(first)
            raise le.APIConnectionError(message="dropped", llm_provider="openrouter", model="gpt")

    fake = _FailingStream(["a"])

    async def fake_acompletion(**kwargs: Any) -> _FailingStream:
        fake.__aiter__()
        return fake

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gw = LLMGateway(_settings())

    with pytest.raises(DependencyError):
        async for _ in gw.stream([ChatMessage(role=Role.USER, content="hi")]):
            pass
    assert fake.closed is True


# --- AC-8: boundary — only domain types cross; litellm only under app/llm/ --


async def test_no_vendor_type_crosses_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returned objects are domain dataclasses, never litellm/openai objects."""

    async def fake_acompletion(**kwargs: Any) -> _Response:
        return _Response("hi", usage=_Usage(1, 1, 2))

    async def fake_aembedding(**kwargs: Any) -> _EmbeddingResponse:
        return _EmbeddingResponse([[0.0, 1.0]])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
    gw = LLMGateway(_settings())

    completion = await gw.chat([ChatMessage(role=Role.USER, content="hi")])
    assert type(completion).__module__ == "app.domain.llm"
    assert type(completion.usage).__module__ == "app.domain.llm"

    embeddings = await gw.embed(["a"])
    assert type(embeddings[0]).__module__ == "app.domain.llm"


def test_litellm_imported_only_under_app_llm() -> None:
    """Static boundary check: no module outside app/llm/ imports litellm.

    Mirrors the ADR-0004 rule that ``backend/app/llm/`` is the sole importer of
    the LiteLLM client. Scans the app source tree for ``litellm`` imports.
    """
    app_root = Path(__file__).resolve().parent.parent / "app"
    offenders: list[str] = []
    for py in app_root.rglob("*.py"):
        rel = py.relative_to(app_root)
        if rel.parts and rel.parts[0] == "llm":
            continue  # the gateway is allowed to import litellm
        text = py.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("import litellm") or stripped.startswith("from litellm"):
                offenders.append(f"{rel}: {stripped}")
    assert offenders == [], f"litellm imported outside app/llm/: {offenders}"


# --- Key-gated live smoke ---------------------------------------------------
#
# These open a REAL outbound socket to OpenRouter, so they are gated twice and
# skipped by default (issue #94): (1) an explicit opt-in env flag ``RUN_LIVE=1``,
# AND (2) an ``OPENROUTER_API_KEY`` present. The default offline ``uv run pytest``
# (no flag, no key) therefore opens no external socket — collected-but-skipped.
#
# Even when they DO run, the ``_live_gateway`` fixture closes LiteLLM's reusable
# process-global async clients in teardown, so no socket leaks into a later test
# (the leak that ``filterwarnings = error`` used to mis-blame on the next test —
# the rotating failure characterized in issue #94's root-cause comment).

_RUN_LIVE = os.environ.get("RUN_LIVE", "").strip() not in ("", "0", "false", "False")
_HAS_KEY = bool(os.environ.get("OPENROUTER_API_KEY", "").strip())

live = pytest.mark.skipif(
    not (_RUN_LIVE and _HAS_KEY),
    reason=(
        "live smoke opted out: needs RUN_LIVE=1 AND OPENROUTER_API_KEY "
        f"(RUN_LIVE={'set' if _RUN_LIVE else 'unset'}, "
        f"key={'set' if _HAS_KEY else 'unset'}) — skipped (offline-safe, #94)."
    ),
)


@pytest.fixture
async def _live_gateway() -> AsyncIterator[LLMGateway]:
    """A gateway whose LiteLLM clients are closed in teardown (#94).

    The finalizer runs ``aclose_litellm_clients`` so the real sockets opened by
    the live call are released deterministically — not on late GC, which under
    ``filterwarnings = error`` would raise ``PytestUnraisableExceptionWarning:
    ResourceWarning: unclosed socket`` against a subsequent test.
    """
    settings = Settings()  # type: ignore[call-arg]  # reads real env / .env
    try:
        yield LLMGateway(settings)
    finally:
        await aclose_litellm_clients()


@live
@pytest.mark.live
async def test_live_openrouter_chat_smoke(_live_gateway: LLMGateway) -> None:
    """One tiny real OpenRouter call end-to-end (skipped unless opted in)."""
    result = await _live_gateway.chat(
        [ChatMessage(role=Role.USER, content="Reply with the single word: pong")]
    )
    assert isinstance(result, Completion)
    assert result.content.strip() != ""
    assert result.usage.total_tokens >= 0


@live
@pytest.mark.live
async def test_live_embed_smoke(_live_gateway: LLMGateway) -> None:
    """One tiny real embedding call (skipped unless opted in).

    OpenRouter serves embeddings on an OpenAI-compatible endpoint (issue #32);
    the gateway routes ``LLM_EMBEDDING_MODEL`` (default ``openai/baai/bge-m3``)
    through ``LLM_EMBEDDING_API_BASE``. The returned vector width must equal the
    configured ``LLM_EMBEDDING_DIMENSIONS`` so a model/dim mismatch fails here,
    not deep in ingestion.
    """
    settings = Settings()  # type: ignore[call-arg]
    embeddings = await _live_gateway.embed(["lumen copilot smoke test"])
    assert len(embeddings) == 1
    assert len(embeddings[0].vector) == settings.llm_embedding_dimensions


@live
@pytest.mark.live
async def test_live_prompt_cache_second_turn_reads_cache(_live_gateway: LLMGateway) -> None:
    """#411 AC-1: two identical-prefix tool turns against a real Anthropic
    model — the second turn's usage must report cache reads (or, at minimum,
    the first must report a cache write that the second does not repeat).

    The prefix must clear Anthropic's ~1024-token minimum cacheable size, so
    the system message is padded well past it. Costs two small haiku calls;
    gated like every live smoke (RUN_LIVE=1 + key).
    """
    from app.domain.llm import ToolSpec

    filler = " ".join(
        f"Fact {i}: the lumen copilot cache smoke sentence number {i} pads the prefix."
        for i in range(220)
    )
    messages = [
        ChatMessage(role=Role.SYSTEM, content=f"You answer tersely. Context: {filler}"),
        ChatMessage(role=Role.USER, content="Reply with the single word: pong"),
    ]
    tools = (
        ToolSpec(
            name="noop",
            description="never call this",
            parameters={"type": "object", "properties": {}},
        ),
    )

    async def _turn_usage() -> TokenUsage | None:
        usage: TokenUsage | None = None
        async for ev in _live_gateway.stream_tools(
            messages,
            tools=tools,
            model="openrouter/anthropic/claude-haiku-4.5",
            tool_choice="none",
            cache_key="lumen-cache-smoke",
        ):
            usage = ev.usage or usage
        return usage

    first = await _turn_usage()
    second = await _turn_usage()
    assert first is not None and second is not None
    assert first.prompt_tokens > 1024  # the prefix actually clears the minimum
    # AC-1: the second turn hits the cache written by the first.
    assert second.cached_prompt_tokens > 0 or (
        first.cache_write_tokens > 0 and second.cache_write_tokens == 0
    )


# --- #94 regression: the gateway's client-close teardown is real ------------


async def test_aclose_litellm_clients_closes_module_client_and_clears_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``aclose_litellm_clients`` releases LiteLLM's reusable async clients (#94).

    Proves the teardown hook the live fixture relies on actually closes the
    process-global ``module_level_aclient`` and clears the per-model client
    cache — so the live test cannot leak an unclosed socket into the next test,
    regardless of ordering. Uses fakes so it runs offline (no real socket).
    """
    import litellm

    from app.llm.gateway import aclose_litellm_clients

    closed: list[str] = []

    class _FakeHandler:
        def __init__(self, tag: str) -> None:
            self._tag = tag

        async def close(self) -> None:
            closed.append(self._tag)

    class _FakeCache:
        def __init__(self) -> None:
            self.cache_dict: dict[str, _FakeHandler] = {
                "model-a": _FakeHandler("model-a"),
                "model-b": _FakeHandler("model-b"),
            }

    monkeypatch.setattr(litellm, "module_level_aclient", _FakeHandler("module"))
    fake_cache = _FakeCache()
    monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", fake_cache)

    await aclose_litellm_clients()

    # The module-level client and every cached per-model client were closed...
    assert set(closed) == {"module", "model-a", "model-b"}
    # ...and the cache was cleared so a later call rebuilds fresh clients.
    assert fake_cache.cache_dict == {}

    # Idempotent: a second call against the now-empty cache is a safe no-op.
    closed.clear()
    await aclose_litellm_clients()
    assert closed == ["module"]


# --- #413 (ADR-0016 §4): retryability is classified in the gateway ----------


def _classify(exc: Exception) -> AppError:
    from app.llm.gateway import _map_vendor_error

    return _map_vendor_error(exc)


def test_transient_faults_classify_retryable() -> None:
    """Timeout / connection / 429 / provider 5xx → a RETRYABLE LlmProviderError
    (still the 503 DependencyError surface — handlers unchanged)."""
    from app.llm import LlmProviderError

    for exc in (
        le.Timeout("m", model="x", llm_provider="p"),
        le.APIConnectionError("m", llm_provider="p", model="x"),
        le.RateLimitError("m", llm_provider="p", model="x"),
        le.InternalServerError("m", llm_provider="p", model="x"),
        le.ServiceUnavailableError("m", llm_provider="p", model="x"),
    ):
        mapped = _classify(exc)
        assert isinstance(mapped, LlmProviderError), type(exc).__name__
        assert isinstance(mapped, DependencyError)
        assert mapped.retryable is True, type(exc).__name__
        # Never the vendor MESSAGE (it may carry the api_key) — class name only.
        assert "m" != mapped.detail
        assert type(exc).__name__ in (mapped.detail or "")


def test_auth_and_unknown_faults_classify_terminal() -> None:
    """Auth/permission — and anything unclassifiable — must NOT retry: a retry
    cannot fix a bad key, and hammering an unknown failure is worse than
    failing fast (ADR-0016 §4)."""
    from app.llm import LlmProviderError

    auth = _classify(le.AuthenticationError("m", llm_provider="p", model="x"))
    assert isinstance(auth, LlmProviderError) and auth.retryable is False
    unknown = _classify(RuntimeError("totally unexpected"))
    assert isinstance(unknown, LlmProviderError) and unknown.retryable is False


def test_bad_request_still_maps_to_validation_error() -> None:
    """The 422 lane is untouched by #413: request-shaped faults stay
    ValidationError (never retried, never a 503)."""
    mapped = _classify(le.BadRequestError("m", model="x", llm_provider="p"))
    assert isinstance(mapped, ValidationError)


def test_retry_after_hint_parsed_defensively() -> None:
    """A numeric Retry-After rides the classified error; date-form and garbage
    are ignored (the header is attacker-adjacent input). The CAP is the retry
    loop's job — the gateway only reports."""
    from app.llm import LlmProviderError

    class _Headers:
        def __init__(self, value: object) -> None:
            self._value = value

        def get(self, _name: str) -> object:
            return self._value

    class _Resp:
        def __init__(self, value: object) -> None:
            self.headers = _Headers(value)

    def with_header(value: object) -> LlmProviderError:
        exc = le.RateLimitError("m", llm_provider="p", model="x")
        exc.response = _Resp(value)  # type: ignore[assignment]
        mapped = _classify(exc)
        assert isinstance(mapped, LlmProviderError)
        return mapped

    assert with_header("7").retry_after_seconds == 7.0
    assert with_header(3).retry_after_seconds == 3.0
    assert with_header("Wed, 21 Oct 2026 07:28:00 GMT").retry_after_seconds is None
    assert with_header("-5").retry_after_seconds is None
    assert with_header(None).retry_after_seconds is None


def test_retry_after_hint_contains_hostile_objects() -> None:
    """#440 finding 5: raising ``headers`` properties, raising ``get``, raising
    ``__str__``, and non-finite numerics must all be CONTAINED — the 429 stays
    the typed retryable error, never an escaping exception."""
    from app.llm import LlmProviderError

    class _RaisingStr:
        def __str__(self) -> str:
            raise RuntimeError("hostile __str__")

    class _RaisingGet:
        def get(self, _name: str) -> object:
            raise RuntimeError("hostile get")

    class _RaisingHeaders:
        @property
        def headers(self) -> object:
            raise RuntimeError("hostile headers property")

    def rl_with_response(response: object) -> Exception:
        exc = le.RateLimitError("m", llm_provider="p", model="x")
        exc.response = response  # type: ignore[assignment]
        return exc

    class _Resp:
        def __init__(self, headers: object) -> None:
            self.headers = headers

    class _HeadersOf:
        def __init__(self, value: object) -> None:
            self._value = value

        def get(self, _name: str) -> object:
            return self._value

    cases = [
        rl_with_response(_Resp(_HeadersOf(_RaisingStr()))),
        rl_with_response(_Resp(_RaisingGet())),
        rl_with_response(_RaisingHeaders()),
        rl_with_response(_Resp(_HeadersOf("inf"))),
        rl_with_response(_Resp(_HeadersOf("nan"))),
    ]
    for exc in cases:
        mapped = _classify(exc)
        assert isinstance(mapped, LlmProviderError)
        assert mapped.retryable is True
        assert mapped.retry_after_seconds is None
