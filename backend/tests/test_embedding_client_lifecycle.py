"""Loop ownership regressions for the embedding gateway (#780)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import litellm
import openai
import pytest

from app.llm import LLMGateway
from app.tasks.runner import run_task
from tests.test_llm_gateway import _settings


@pytest.fixture
def loop_clients(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    clients: list[Any] = []
    unowned: list[Any] = []

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            self.loop = asyncio.get_running_loop()
            self.closed = False
            self.kwargs = kwargs
            clients.append(self)

        async def close(self) -> None:
            assert asyncio.get_running_loop() is self.loop
            assert not self.loop.is_closed()
            self.closed = True

    async def embedding(**kwargs: Any) -> Any:
        client = kwargs.get("client")
        if client is None:
            # Mirror pinned LiteLLM's cache: it omits the owning event loop.
            if not unowned:
                unowned.append(Client())
            client = unowned[0]
        if client.loop is not asyncio.get_running_loop() or client.closed:
            raise RuntimeError("event loop is closed")
        return SimpleNamespace(data=[{"index": 0, "embedding": [0.1]}])

    async def no_io() -> None:
        pass

    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    monkeypatch.setattr(litellm, "aembedding", embedding)
    monkeypatch.setattr("app.tasks.runner.dispose_engine", no_io)
    monkeypatch.setattr("app.tasks.rate_limit.aclose_async_rate_limit_pools", no_io)
    return clients


def test_two_real_task_runner_loops_retire_embedding_clients(loop_clients: list[Any]) -> None:
    async def task(text: str) -> None:
        gateway = LLMGateway(_settings())
        assert len(await gateway.embed([text])) == 1
        # Fresh gateway instances on this loop share the same owned transport.
        assert len(await LLMGateway(_settings()).embed([text + " again"])) == 1

    run_task(task("first task"))
    run_task(task("second task"))
    assert len(loop_clients) == 2
    assert all(client.closed for client in loop_clients)
    assert loop_clients[0].loop is not loop_clients[1].loop
    assert all(
        client.kwargs["base_url"] == "https://openrouter.ai/api/v1" for client in loop_clients
    )
    assert all(client.kwargs["timeout"] == 60 for client in loop_clients)
    expected_retries: int = litellm.DEFAULT_MAX_RETRIES  # type: ignore[attr-defined]
    assert all(client.kwargs["max_retries"] == expected_retries for client in loop_clients)


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_task_failure_or_cancellation_still_closes_owned_client(
    loop_clients: list[Any], failure: type[BaseException]
) -> None:
    async def task() -> None:
        await LLMGateway(_settings()).embed(["synthetic failure cleanup"])
        raise failure()

    with pytest.raises(failure):
        run_task(task())
    assert len(loop_clients) == 1 and loop_clients[0].closed

    async def retry() -> None:
        await LLMGateway(_settings()).embed(["next task after failure"])

    run_task(retry())
    assert len(loop_clients) == 2 and all(client.closed for client in loop_clients)


def test_native_openai_defaults_and_explicit_overrides_are_preserved(
    loop_clients: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(litellm, "api_base", None)
    monkeypatch.setattr(litellm, "organization", None)
    monkeypatch.setenv("OPENAI_API_BASE", "https://native-openai.example/v1")
    monkeypatch.setenv("OPENAI_ORGANIZATION", "synthetic-organization")

    async def task() -> None:
        await LLMGateway(_settings(LLM_EMBEDDING_API_BASE="")).embed(["native defaults"])
        await LLMGateway(_settings()).embed(
            ["explicit overrides"], api_base="https://override.example/v1", api_key="synthetic-key"
        )

    run_task(task())
    assert len(loop_clients) == 2
    assert loop_clients[0].kwargs["base_url"] == "https://native-openai.example/v1"
    assert loop_clients[0].kwargs["organization"] == "synthetic-organization"
    assert loop_clients[1].kwargs["base_url"] == "https://override.example/v1"
    assert loop_clients[1].kwargs["api_key"] == "synthetic-key"
    assert all(client.closed for client in loop_clients)


def test_non_openai_adapter_and_borrowed_http_transport_keep_generic_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def provider(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(data=[{"index": 0, "embedding": [0.1]}])

    monkeypatch.setattr(litellm, "aembedding", provider)

    async def task() -> None:
        await LLMGateway(_settings()).embed(["generic provider"], model="cohere/embed-english-v3.0")
        monkeypatch.setattr(litellm, "aclient_session", object())
        await LLMGateway(_settings()).embed(["externally owned transport"])

    run_task(task())
    assert len(calls) == 2
    assert all("client" not in call for call in calls)
    assert calls[0]["model"] == "cohere/embed-english-v3.0"


def test_cleanup_on_one_loop_does_not_touch_another_active_loop_or_vendor_cache(
    loop_clients: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import concurrent.futures
    import threading

    ready = threading.Event()
    release = threading.Event()
    sentinel = object()
    vendor_cache = {"synthetic-unrelated-cache-entry": sentinel}
    monkeypatch.setattr(
        litellm, "in_memory_llm_clients_cache", SimpleNamespace(cache_dict=vendor_cache)
    )

    async def background() -> None:
        await LLMGateway(_settings()).embed(["other live loop"])
        ready.set()
        assert await asyncio.to_thread(release.wait, 10)
        # The main thread has completed and retired its own loop by now.
        assert not loop_clients[0].closed
        await LLMGateway(_settings()).embed(["other live loop still works"])

    async def foreground() -> None:
        await LLMGateway(_settings()).embed(["foreground task"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_task, background())
        try:
            assert ready.wait(timeout=10)
            run_task(foreground())
            assert len(loop_clients) == 2
            assert not loop_clients[0].closed and loop_clients[1].closed
            assert vendor_cache == {"synthetic-unrelated-cache-entry": sentinel}
        finally:
            release.set()
        future.result(timeout=10)
    assert all(client.closed for client in loop_clients)


def test_provider_failure_stays_typed_and_cleanup_failure_cannot_mask_it(
    loop_clients: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import litellm.exceptions as errors

    from app.llm import LlmProviderError

    calls = 0

    async def provider(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise errors.RateLimitError(
            message="synthetic private marker", model="synthetic", llm_provider="openai"
        )

    monkeypatch.setattr(litellm, "aembedding", provider)

    async def task() -> None:
        try:
            await LLMGateway(_settings()).embed(["synthetic provider failure"])
        except LlmProviderError as error:

            async def close_failure() -> None:
                raise RuntimeError("synthetic close failure")

            loop_clients[-1].close = close_failure
            assert error.retryable is True
            assert "synthetic private marker" not in (error.detail or "")
            raise

    with pytest.raises(LlmProviderError):
        run_task(task())
    assert calls == 1  # lifecycle handling never inserts provider retries
    assert len(loop_clients) == 1

    async def retry() -> None:
        with pytest.raises(LlmProviderError):
            await LLMGateway(_settings()).embed(["synthetic subsequent failure"])

    run_task(retry())
    assert len(loop_clients) == 2  # failed close retired its registry entry


def test_native_openai_without_env_base_uses_the_existing_default(
    loop_clients: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(litellm, "api_base", None)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)

    async def task() -> None:
        await LLMGateway(_settings(LLM_EMBEDDING_API_BASE="")).embed(["native endpoint"])

    run_task(task())
    assert loop_clients[0].kwargs["base_url"] == "https://api.openai.com/v1"


@pytest.mark.live
def test_live_two_sequential_real_task_runner_embeddings() -> None:
    """Two real provider calls, no test-side SDK cache mutation or substituted loop."""
    import contextlib
    import io
    import logging
    import os

    from app.core.config import get_settings

    if os.environ.get("RUN_LIVE") != "1":
        pytest.skip("set RUN_LIVE=1 with the configured real embedding provider")
    settings = get_settings()
    if not settings.llm_enabled:
        pytest.skip("real embedding provider is not configured")
    dimensions: list[int] = []

    async def task(number: int) -> None:
        result = await LLMGateway(settings).embed([f"Synthetic QA780 sequential task {number}"])
        dimensions.append(len(result[0].vector))

    previous_logging = logging.root.manager.disable
    try:
        # Provider diagnostics must not leak credentials, vectors or raw errors
        # into a failing live-test report. The public result is dimensions only.
        logging.disable(logging.CRITICAL)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            run_task(task(1))
            run_task(task(2))
    except Exception as error:
        pytest.fail(f"sequential embedding failed: {type(error).__name__}", pytrace=False)
    finally:
        logging.disable(previous_logging)
    if dimensions != [settings.llm_embedding_dimensions] * 2:
        pytest.fail("sequential embedding returned an unexpected dimension count", pytrace=False)
    print(f"two real task loops succeeded; dimensions={dimensions}")


async def test_app_shutdown_retires_current_loop_clients_only_after_answer_drain(
    loop_clients: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import FastAPI

    import app.main as main
    from app.llm import aclose_embedding_clients

    order: list[str] = []

    async def no_io(*args: Any, **kwargs: Any) -> None:
        pass

    async def drain(*args: Any, **kwargs: Any) -> None:
        assert not loop_clients[0].closed
        order.append("drain")

    async def close() -> None:
        await aclose_embedding_clients()
        order.append("embedding")

    monkeypatch.setattr(main, "get_object_store", lambda: SimpleNamespace(ensure_bucket=no_io))
    monkeypatch.setattr(main, "provision_embedding_contract", no_io)
    monkeypatch.setattr(main, "_drain_answer_tasks", drain)
    monkeypatch.setattr(main, "aclose_embedding_clients", close)
    for name in (
        "aclose_search_store",
        "aclose_backplane",
        "aclose_async_rate_limit_pools",
        "dispose_engine",
    ):
        monkeypatch.setattr(main, name, no_io)
    app = FastAPI()
    app.state.settings = _settings()
    async with main.lifespan(app):
        await LLMGateway(_settings()).embed(["app lifespan ownership"])
        assert not loop_clients[0].closed
    assert order == ["drain", "embedding"]
    assert loop_clients[0].closed
