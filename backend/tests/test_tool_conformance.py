"""Protocol-conformance probes for the governed tool-calling gateway."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import subprocess
import sys
import textwrap
import threading
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.domain.llm import ChatMessage, Role, StreamClosure, StreamEvent, ToolCall, ToolSpec

_CASES = ("exact_once", "parallel", "round_trip", "cancellation")
_MODEL = "vendor/small-test-model"
_ROUTE = "route-revision-17"
_LEAK = "provider-secret-should-not-escape"


class _ManualDeadlines:
    """Fire timer callbacks only after the tested stream reaches a barrier."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.pending: asyncio.Queue[tuple[float, asyncio.TimerHandle]] = asyncio.Queue()
        self.loop = asyncio.get_running_loop()
        clock = self.loop.time()
        monkeypatch.setattr(self.loop, "time", lambda: clock)
        monkeypatch.setattr(self.loop, "call_later", self.call_later)
        monkeypatch.setattr(self.loop, "call_at", self.call_at)

    def call_at(
        self, when: float, callback: Any, *args: Any, context: Any = None
    ) -> asyncio.TimerHandle:
        return self.call_later(when - self.loop.time(), callback, *args, context=context)

    def call_later(
        self, delay: float, callback: Any, *args: Any, context: Any = None
    ) -> asyncio.TimerHandle:
        handle = asyncio.TimerHandle(self.loop.time() + delay, callback, args, self.loop, context)
        self.pending.put_nowait((delay, handle))
        return handle

    def fire(self) -> float:
        delay, handle = self.pending.get_nowait()
        assert not handle.cancelled()
        handle._run()
        return delay


class _BlockedStream:
    def __init__(self, *, block_unwind: bool = False) -> None:
        self.block_unwind = block_unwind
        self.read_started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.read_finished = asyncio.Event()
        self.close_finished = asyncio.Event()
        self.read_task: asyncio.Task[Any] | None = None
        self.close_task: asyncio.Task[Any] | None = None

    def __aiter__(self) -> _BlockedStream:
        return self

    async def __anext__(self) -> StreamEvent:
        self.read_task = asyncio.current_task()
        self.read_started.set()
        try:
            await self.release.wait()
        finally:
            try:
                if self.block_unwind:
                    await self.aclose()
            finally:
                self.read_finished.set()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        if self.close_finished.is_set():
            return
        self.close_task = asyncio.current_task()
        self.close_started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.close_cancelled.set()
            raise
        finally:
            self.close_finished.set()


@pytest.mark.parametrize("block_unwind", [False, True], ids=["close", "iterator-unwind"])
@pytest.mark.asyncio
async def test_deadline_bounds_stalled_cleanup_without_waiting_for_release(
    monkeypatch: pytest.MonkeyPatch, block_unwind: bool
) -> None:
    deadlines = _ManualDeadlines(monkeypatch)
    stream = _BlockedStream(block_unwind=block_unwind)
    gateway = _SyntheticGateway()
    original = gateway.stream_tools

    def stream_tools(messages: Sequence[ChatMessage], **kwargs: Any) -> AsyncIterator[StreamEvent]:
        if gateway._probe_name(messages) == "exact_once":
            return stream
        return original(messages, **kwargs)

    monkeypatch.setattr(gateway, "stream_tools", stream_tools)
    task = asyncio.create_task(
        _service()(gateway, model=_MODEL, route_revision=_ROUTE, timeout_seconds=0.5)
    )
    try:
        await stream.read_started.wait()
        assert deadlines.fire() == 0.5
        await stream.close_started.wait()
        # A close barrier proves the old deadline has fired. There must now be
        # a NEW independent cleanup deadline, before releasing any provider I/O.
        assert not deadlines.pending.empty(), "cleanup has no independent deadline"
        assert 0 < deadlines.fire() <= 1
        await stream.close_cancelled.wait()
        report = await task
        assert not stream.release.is_set()
        assert not report.qualified
        assert _case(report, "exact_once").reason == "timeout"
        assert all(case.reason == "cleanup_timeout" for case in report.cases[1:])
        assert stream.read_finished.is_set()
        assert stream.close_finished.is_set()
        assert stream.read_task is not None and stream.read_task.done()
        assert stream.close_task is not None and stream.close_task.done()
    finally:
        stream.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _service() -> Any:
    # Keep collection independent of the implementation landing in parallel.
    from app.services.tools.conformance import qualify

    return qualify


def _case(report: Any, name: str) -> Any:
    return next(case for case in report.cases if case.name == name)


class _SyntheticGateway:
    """A deterministic ``stream_tools`` fake; it never opens a network socket."""

    def __init__(self, mode: str = "correct") -> None:
        self.mode = mode
        self.calls: list[dict[str, Any]] = []
        self.emitted: list[tuple[str, tuple[ToolCall, ...]]] = []
        self.early_closed: set[str] = set()
        self.user_prompts: list[str] = []
        self.blocked = asyncio.Event()

    @staticmethod
    def _probe_name(messages: Sequence[ChatMessage]) -> str:
        for message in reversed(messages):
            if message.role is Role.USER:
                content = message.content.lower()
                for name in _CASES:
                    if name in content:
                        return name
        return "exact_once"

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
        max_tokens: int | None = None,
        closure: StreamClosure | None = None,
    ) -> AsyncIterator[StreamEvent]:
        case = self._probe_name(messages)
        self.calls.append(
            {
                "case": case,
                "messages": tuple(messages),
                "tools": tuple(tools),
                "model": model,
                "tool_choice": tool_choice,
                "api_key": api_key,
                "api_base": api_base,
                "cache_key": cache_key,
                "max_tokens": max_tokens,
            }
        )
        self.user_prompts.extend(
            message.content for message in messages if message.role is Role.USER
        )
        completed = False
        try:
            if self.mode == "secret_error":
                raise RuntimeError(f"provider failed with {_LEAK}")

            if self.mode == "timeout" and case == "exact_once":
                yield StreamEvent(text="stream-open")
                self.blocked.set()
                await asyncio.Event().wait()

            if case == "cancellation":
                yield StreamEvent(text="stream-open")
                # qualify() must close this iterator after inspecting its first event.
                await asyncio.Event().wait()

            if self.mode == "third_turn_loop":
                loop_call = ToolCall(
                    id=f"loop-{len([call for call in self.calls if call['case'] == case])}",
                    name=tools[0].name,
                    arguments={"label": "alpha", "comment": None},
                )
                self.emitted.append((case, (loop_call,)))
                yield StreamEvent(tool_calls=(loop_call,), finish_reason="tool_calls")
                completed = True
                return

            tool_messages = [message.content for message in messages if message.role is Role.TOOL]
            if tool_messages:
                if self.mode == "repeat_when_enabled" and tool_choice != "none":
                    yield StreamEvent(
                        tool_calls=(
                            ToolCall(
                                id="repeat-alpha",
                                name=tools[0].name,
                                arguments={"label": "alpha", "comment": None},
                            ),
                        ),
                        finish_reason="tool_calls",
                    )
                    completed = True
                    return
                if self.mode == "ignore_result":
                    yield StreamEvent(text="Completed without using any tool output.")
                else:
                    yield StreamEvent(text="\n".join(tool_messages))
                yield StreamEvent(finish_reason="stop")
                completed = True
                return

            if self.mode == "prose_only":
                yield StreamEvent(text="TOOL_OK")
                yield StreamEvent(finish_reason="stop")
                completed = True
                return

            tool_name = tools[0].name
            arguments: dict[str, Any] = {"label": "alpha", "comment": None}
            if self.mode == "invalid_arguments":
                arguments = {"label": "alpha"}
            if self.mode == "unknown_tool":
                tool_name = "not_the_advertised_tool"

            calls = [ToolCall(id="call-alpha", name=tool_name, arguments=arguments)]
            if case == "parallel" and self.mode != "parallel_single":
                calls.append(
                    ToolCall(
                        id="call-beta",
                        name=tool_name,
                        arguments={"label": "beta", "comment": None},
                    )
                )
            if self.mode == "duplicate_call":
                calls.append(ToolCall(id="call-alpha", name=tool_name, arguments=arguments))
            self.emitted.append((case, tuple(calls)))
            yield StreamEvent(tool_calls=tuple(calls), finish_reason="tool_calls")
            completed = True
        finally:
            if not completed:
                self.early_closed.add(case)
            if closure is not None:
                closure.closed = True


@pytest.mark.asyncio
async def test_qualify_reports_the_required_protocol_cases_and_safe_tool_schema() -> None:
    gateway = _SyntheticGateway()
    report = await _service()(gateway, model=_MODEL, route_revision=_ROUTE)

    assert report.suite_version == "tool-protocol-v2"
    assert report.tested_at.tzinfo is UTC
    assert {case.name for case in report.cases} == set(_CASES)
    assert report.qualified
    for case in report.cases:
        assert case.passed, case.reason
    assert all(call["max_tokens"] == 256 for call in gateway.calls)

    call = gateway.calls[0]
    assert call["model"] == _MODEL
    assert call["tools"]
    name = call["tools"][0].name
    assert len(name) == 64
    assert re.fullmatch(r"[A-Za-z0-9_]{64}", name)
    schema = call["tools"][0].parameters
    assert set(schema["required"]) >= {"label", "comment"}
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"label", "comment"}
    assert schema["properties"]["label"]["enum"] == ["alpha", "beta"]
    assert schema["properties"]["comment"]["type"] == ["string", "null"]
    exact_calls = [calls for case, calls in gateway.emitted if case == "exact_once"]
    assert len(exact_calls) == 1
    assert len(exact_calls[0]) == 1
    assert exact_calls[0][0].arguments == {"label": "alpha", "comment": None}
    assert exact_calls[0][0].name == name
    parallel = [calls for case, calls in gateway.emitted if case == "parallel"]
    assert len(parallel) == 1 and len(parallel[0]) == 2
    assert [call.arguments["label"] for call in parallel[0]] == ["alpha", "beta"]
    assert [call.arguments["comment"] for call in parallel[0]] == [None, None]
    assert len({call.id for call in parallel[0]}) == 2


@pytest.mark.asyncio
async def test_exact_once_requires_one_well_shaped_call_not_successful_prose() -> None:
    report = await _service()(_SyntheticGateway("prose_only"), model=_MODEL, route_revision=_ROUTE)

    assert not _case(report, "exact_once").passed
    assert not report.qualified


@pytest.mark.parametrize("mode", ["duplicate_call", "unknown_tool", "invalid_arguments"])
@pytest.mark.asyncio
async def test_exact_once_rejects_duplicate_unknown_or_invalid_calls(mode: str) -> None:
    report = await _service()(_SyntheticGateway(mode), model=_MODEL, route_revision=_ROUTE)

    assert not _case(report, "exact_once").passed
    assert not report.qualified


@pytest.mark.asyncio
async def test_round_trip_requires_using_the_unpredictable_tool_result() -> None:
    gateway = _SyntheticGateway("ignore_result")
    report = await _service()(gateway, model=_MODEL, route_revision=_ROUTE)

    assert not _case(report, "round_trip").passed
    assert not report.qualified


@pytest.mark.asyncio
async def test_parallel_is_reported_but_is_not_a_qualification_requirement() -> None:
    report = await _service()(
        _SyntheticGateway("parallel_single"), model=_MODEL, route_revision=_ROUTE
    )

    assert not _case(report, "parallel").passed
    assert report.qualified


@pytest.mark.asyncio
async def test_third_tool_turn_is_bounded_and_fails_the_probe() -> None:
    gateway = _SyntheticGateway("third_turn_loop")
    report = await _service()(gateway, model=_MODEL, route_revision=_ROUTE)

    assert not report.qualified
    call_counts = {case: sum(call["case"] == case for call in gateway.calls) for case in _CASES}
    assert call_counts["exact_once"] == 2
    assert all(count <= 2 for count in call_counts.values())


@pytest.mark.asyncio
async def test_result_turn_detects_repeat_calls_when_gateway_honors_forced_none() -> None:
    gateway = _SyntheticGateway("repeat_when_enabled")
    report = await _service()(gateway, model=_MODEL, route_revision=_ROUTE)

    assert not report.qualified
    for name in ("exact_once", "parallel", "round_trip"):
        assert _case(report, name).reason == "turn_budget"
        requests = [call for call in gateway.calls if call["case"] == name]
        assert len(requests) == 2
        assert requests[1]["tool_choice"] == "auto"
        assert sum(message.role is Role.TOOL for message in requests[1]["messages"]) == (
            2 if name == "parallel" else 1
        )


@pytest.mark.asyncio
async def test_cancellation_closes_the_synthetic_stream() -> None:
    gateway = _SyntheticGateway()
    report = await _service()(gateway, model=_MODEL, route_revision=_ROUTE)

    assert _case(report, "cancellation").passed
    assert "cancellation" in gateway.early_closed


@pytest.mark.asyncio
async def test_cancellation_rejects_a_stream_without_a_close_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _SyntheticGateway()
    original = gateway.stream_tools
    active: list[AsyncIterator[StreamEvent]] = []

    class Unclosable:
        def __init__(self, inner: AsyncIterator[StreamEvent]) -> None:
            self.inner = inner

        def __aiter__(self) -> Unclosable:
            return self

        async def __anext__(self) -> StreamEvent:
            return await anext(self.inner)

    def stream_tools(messages: Sequence[ChatMessage], **kwargs: Any) -> AsyncIterator[StreamEvent]:
        stream = original(messages, **kwargs)
        if gateway._probe_name(messages) == "cancellation":
            active.append(stream)
            return Unclosable(stream)
        return stream

    monkeypatch.setattr(gateway, "stream_tools", stream_tools)
    try:
        report = await _service()(gateway, model=_MODEL, route_revision=_ROUTE)
        assert not report.qualified
        assert _case(report, "cancellation").reason == "stream_close_unsupported"
        assert "cancellation" not in gateway.early_closed
    finally:
        for stream in active:
            await stream.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cancellation_probe_cancels_a_real_consumer_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _SyntheticGateway()
    original = gateway.stream_tools
    cancellation_counts: list[int] = []

    class RecordingStream:
        def __init__(self, closure: Any) -> None:
            self.closure = closure

        def __aiter__(self) -> RecordingStream:
            return self

        async def __anext__(self) -> StreamEvent:
            return StreamEvent(text="stream-open")

        async def aclose(self) -> None:
            task = asyncio.current_task()
            assert task is not None
            cancellation_counts.append(task.cancelling())
            if self.closure is not None:
                self.closure.closed = True

    def stream_tools(messages: Sequence[ChatMessage], **kwargs: Any) -> AsyncIterator[StreamEvent]:
        if gateway._probe_name(messages) == "cancellation":
            return RecordingStream(kwargs.get("closure"))
        return original(messages, **kwargs)

    monkeypatch.setattr(gateway, "stream_tools", stream_tools)
    report = await _service()(gateway, model=_MODEL, route_revision=_ROUTE)
    assert report.qualified
    assert cancellation_counts == [1]


@pytest.mark.asyncio
async def test_caller_cancellation_preserves_cancelled_outcome_and_closes_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _BlockedStream()
    gateway = _SyntheticGateway()
    original = gateway.stream_tools

    def stream_tools(messages: Sequence[ChatMessage], **kwargs: Any) -> AsyncIterator[StreamEvent]:
        if gateway._probe_name(messages) == "exact_once":
            return stream
        return original(messages, **kwargs)

    monkeypatch.setattr(gateway, "stream_tools", stream_tools)
    task = asyncio.create_task(_service()(gateway, model=_MODEL, route_revision=_ROUTE))
    try:
        await stream.read_started.wait()
        task.cancel()
        await stream.close_started.wait()
        stream.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert stream.read_finished.is_set()
        assert stream.close_finished.is_set()
        assert not gateway.calls
    finally:
        stream.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_caller_cancellation_bounds_stalled_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadlines = _ManualDeadlines(monkeypatch)
    stream = _BlockedStream()
    gateway = _SyntheticGateway()
    monkeypatch.setattr(gateway, "stream_tools", lambda *args, **kwargs: stream)
    task = asyncio.create_task(_service()(gateway, model=_MODEL, route_revision=_ROUTE))
    try:
        await stream.read_started.wait()
        task.cancel()
        await stream.close_started.wait()
        _, original_deadline = deadlines.pending.get_nowait()
        assert original_deadline.cancelled()
        assert deadlines.fire() == 1
        await stream.close_cancelled.wait()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not stream.release.is_set()
        assert stream.read_finished.is_set()
        assert stream.close_finished.is_set()
        assert task.cancelled()
    finally:
        stream.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_provider_error_reason_does_not_leak_secret_message() -> None:
    report = await _service()(
        _SyntheticGateway("secret_error"), model=_MODEL, route_revision=_ROUTE
    )

    assert not report.qualified
    assert all(_LEAK not in (case.reason or "") for case in report.cases)


@pytest.mark.asyncio
async def test_timeout_is_a_failed_report_and_closes_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadlines = _ManualDeadlines(monkeypatch)
    gateway = _SyntheticGateway("timeout")
    task = asyncio.create_task(
        _service()(gateway, model=_MODEL, route_revision=_ROUTE, timeout_seconds=0.5)
    )
    try:
        await gateway.blocked.wait()
        assert deadlines.fire() == 0.5
        report = await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert not report.qualified
    assert "exact_once" in gateway.early_closed


@pytest.mark.asyncio
async def test_is_current_checks_model_route_age_future_time_and_suite_version() -> None:
    report = await _service()(_SyntheticGateway(), model=_MODEL, route_revision=_ROUTE)
    now = report.tested_at + timedelta(hours=1)

    assert report.is_current(model=_MODEL, route_revision=_ROUTE, now=now)
    assert not report.is_current(model="vendor/other-model", route_revision=_ROUTE, now=now)
    assert not report.is_current(model=_MODEL, route_revision="route-revision-18", now=now)
    assert not report.is_current(
        model=_MODEL,
        route_revision=_ROUTE,
        now=report.tested_at + timedelta(days=7, seconds=1),
    )
    assert not report.is_current(
        model=_MODEL, route_revision=_ROUTE, now=report.tested_at - timedelta(seconds=1)
    )
    old_suite = replace(report, suite_version="tool-protocol-v0")
    assert not old_suite.is_current(model=_MODEL, route_revision=_ROUTE, now=now)


@pytest.mark.parametrize("missing", ["exact_once", "round_trip", "cancellation"])
@pytest.mark.asyncio
async def test_report_requires_each_core_case_exactly_once(missing: str) -> None:
    report = await _service()(_SyntheticGateway(), model=_MODEL, route_revision=_ROUTE)
    incomplete = replace(report, cases=tuple(case for case in report.cases if case.name != missing))
    assert not incomplete.qualified
    assert not incomplete.is_current(_MODEL, _ROUTE, report.tested_at)
    assert not replace(report, cases=report.cases + (report.cases[0],)).qualified
    assert not replace(report, tested_at=report.tested_at.replace(tzinfo=None)).is_current(
        _MODEL, _ROUTE, report.tested_at
    )


@pytest.mark.asyncio
async def test_prior_suite_evidence_is_invalid_after_protocol_corrections() -> None:
    report = await _service()(_SyntheticGateway(), model=_MODEL, route_revision=_ROUTE)
    assert report.suite_version == "tool-protocol-v2"
    assert not replace(report, suite_version="tool-protocol-v1").is_current(
        _MODEL, _ROUTE, report.tested_at
    )


def test_cli_requires_provider_opt_in_before_constructing_gateway(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    script = (
        "import runpy, sys, types\n"
        "class ForbiddenGateway:\n"
        "    def __init__(self):\n"
        "        raise RuntimeError('gateway-opened')\n"
        "sys.modules['app.llm'] = types.SimpleNamespace(LLMGateway=ForbiddenGateway)\n"
        "sys.argv = ['conformance', '--model', 'vendor/synthetic', "
        "'--route-revision', 'route-r1', '--output', "
        f"{str(output)!r}]\n"
        "runpy.run_module('app.services.tools.conformance', run_name='__main__')\n"
    )
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": "postgresql+asyncpg://unused:unused@localhost:1/lumentest_chat",
            "OPENSEARCH_URL": "http://localhost:1",
            "S3_ENDPOINT_URL": "http://localhost:1",
            "REDIS_URL": "redis://localhost:1/0",
            "RUN_LIVE": "0",
            "OPENROUTER_API_KEY": "",
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert "Provider calls require explicit --allow-provider-probes" in result.stderr
    assert "gateway-opened" not in result.stderr
    assert not output.exists()


def test_cli_reports_and_exits_without_provider_cleanup_release(tmp_path: Path) -> None:
    """Exercise actual gateway/CLI shutdown; only LiteLLM and timers are faked."""
    output = tmp_path / "report.json"
    driver = tmp_path / "stalled_cli.py"
    driver.write_text(
        textwrap.dedent(
            """
            import asyncio
            import multiprocessing
            import sys
            import types
            from app.services.tools import conformance

            def configure_gateway(shutdown=None):
                from app.core import config
                original_settings = config.get_settings
                config.get_settings = lambda: original_settings().model_copy(
                    update={"openrouter_api_key": "synthetic-key"}
                )
                read_started = asyncio.Event()
                close_started = asyncio.Event()
                close_cancelled = asyncio.Event()

                class Upstream:
                    def __aiter__(self):
                        return self

                    async def __anext__(self):
                        read_started.set()
                        await asyncio.Event().wait()

                    async def aclose(self):
                        close_started.set()
                        cancellations = 0
                        while True:
                            try:
                                await asyncio.Event().wait()
                            except asyncio.CancelledError:
                                cancellations += 1
                                close_cancelled.set()
                                if cancellations == 2:
                                    # One cancellation from cleanup expiry, then
                                    # another from asyncio.run runner shutdown.
                                    if shutdown is None:
                                        print("UNSUPERVISED_SHUTDOWN", flush=True)
                                        assert sys.stdin.readline().strip() == "release"
                                        return
                                    shutdown.set()

                async def completion(**kwargs):
                    return Upstream()

                sys.modules["litellm"] = types.SimpleNamespace(acompletion=completion)
                original_wait = asyncio.wait

                async def manual_wait(tasks, *, timeout=None, **kwargs):
                    if timeout == 0.5:
                        await read_started.wait()
                        return set(), set(tasks)
                    if timeout == 1:
                        await close_started.wait()
                        return set(), set(tasks)
                    return await original_wait(tasks, timeout=timeout, **kwargs)

                asyncio.wait = manual_wait
                original_qualify = conformance.qualify

                async def qualify(*args, **kwargs):
                    report = await original_qualify(*args, **kwargs)
                    await close_cancelled.wait()
                    print("QUALIFY_RETURNED", flush=True)
                    return report

                conformance.qualify = qualify

            def worker(connection, model, route_revision, timeout_seconds, shutdown):
                configure_gateway(shutdown)
                conformance._provider_worker(connection, model, route_revision, timeout_seconds)

            if __name__ == "__main__":
                if hasattr(conformance, "_provider_worker"):
                    context = multiprocessing.get_context("spawn")
                    shutdown = context.Event()

                    class Receiver:
                        def __init__(self, connection):
                            self.connection = connection

                        def poll(self, timeout):
                            assert 0 < timeout <= 254
                            # Fire the command deadline only once the actual
                            # gateway resists runner-shutdown cancellation.
                            assert shutdown.wait(20), "shutdown barrier not reached"
                            return False

                        def __getattr__(self, name):
                            return getattr(self.connection, name)

                    class Context:
                        def Pipe(self, duplex=False):
                            receiver, sender = context.Pipe(duplex=duplex)
                            return Receiver(receiver), sender

                        def Process(self, *, target, args):
                            return context.Process(target=worker, args=(*args, shutdown))

                    conformance.multiprocessing.get_context = lambda method: Context()
                else:
                    configure_gateway()
                sys.argv = ["conformance", "--model", "vendor/synthetic",
                            "--route-revision", "route-r1", "--timeout", "0.5",
                            "--allow-provider-probes", "--output", sys.argv[1]]
                conformance.main()
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    process = subprocess.Popen(
        [sys.executable, str(driver), str(output)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: queue.Queue[str | None] = queue.Queue()
    assert process.stdout is not None

    def read_lines() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line.strip())
        lines.put(None)

    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()
    released = False
    observed = []
    try:
        while (line := lines.get(timeout=30)) is not None:
            observed.append(line)
            if line == "UNSUPERVISED_SHUTDOWN":
                assert process.stdin is not None
                process.stdin.write("release\n")
                process.stdin.flush()
                released = True
        assert process.wait(timeout=10) == 1
        assert "QUALIFY_RETURNED" in observed
        assert not released, (
            "CLI blocked in asyncio.run shutdown after cleanup deadline; "
            "report required provider release"
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["qualified"] is False
        assert payload["model"] == "vendor/synthetic"
        assert payload["route_revision"] == "route-r1"
        assert {case["name"] for case in payload["cases"]} == set(_CASES)
        assert all(case["reason"] == "command_timeout" for case in payload["cases"])
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        reader.join(timeout=10)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()


@pytest.mark.parametrize("mode", ["correct", "secret_error", "crash"])
def test_cli_supervised_worker_preserves_safe_reports(tmp_path: Path, mode: str) -> None:
    output = tmp_path / "report.json"
    driver = tmp_path / "cooperative_cli.py"
    driver.write_text(
        textwrap.dedent(
            """
            import os
            import sys
            import types
            from app.services.tools import conformance

            def worker(sender, model, route_revision, timeout_seconds):
                if sys.argv[1] == "crash":
                    os._exit(17)
                from tests.test_tool_conformance import _SyntheticGateway
                gateway = _SyntheticGateway(sys.argv[1])
                sys.modules["app.llm"] = types.SimpleNamespace(LLMGateway=lambda settings: gateway)
                conformance._provider_worker(sender, model, route_revision, timeout_seconds)

            if __name__ == "__main__":
                # Preserve argv in the spawned worker; argparse gets explicit
                # arguments so the driver mode is not exposed as a CLI option.
                conformance._provider_worker = worker
                original_parse = conformance.argparse.ArgumentParser.parse_args
                conformance.argparse.ArgumentParser.parse_args = lambda parser: original_parse(
                    parser, ["--model", "vendor/synthetic", "--route-revision", "route-r1",
                             "--allow-provider-probes", "--output", sys.argv[2]]
                )
                conformance.main()
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, str(driver), mode, str(output)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == (0 if mode == "correct" else 1), result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["qualified"] is (mode == "correct")
    assert all(
        case["reason"] == ("passed" if mode == "correct" else "provider_error")
        for case in payload["cases"]
    )
    assert _LEAK not in output.read_text(encoding="utf-8") + result.stdout + result.stderr


def test_cli_module_entrypoint_handles_disabled_provider_without_network(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    env = os.environ.copy()
    env["OPENROUTER_API_KEY"] = ""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.services.tools.conformance",
            "--model",
            "vendor/synthetic",
            "--route-revision",
            "route-r1",
            "--allow-provider-probes",
            "--output",
            str(output),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["qualified"] is False
    # No stream can be confirmed closed when the disabled adapter never opens it.
    assert all(case["reason"] == "stream_close_failed" for case in payload["cases"])
