"""Protocol-conformance probes for the governed tool-calling gateway."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.domain.llm import ChatMessage, Role, StreamEvent, ToolCall, ToolSpec

_CASES = ("exact_once", "parallel", "round_trip", "cancellation")
_MODEL = "vendor/small-test-model"
_ROUTE = "route-revision-17"
_LEAK = "provider-secret-should-not-escape"


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


@pytest.mark.asyncio
async def test_qualify_reports_the_required_protocol_cases_and_safe_tool_schema() -> None:
    gateway = _SyntheticGateway()
    report = await _service()(gateway, model=_MODEL, route_revision=_ROUTE)

    assert report.suite_version == "tool-protocol-v1"
    assert report.tested_at.tzinfo is UTC
    assert {case.name for case in report.cases} == set(_CASES)
    assert report.qualified
    for case in report.cases:
        assert case.passed, case.reason

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
    report = await asyncio.wait_for(
        _service()(gateway, model=_MODEL, route_revision=_ROUTE, timeout_seconds=1),
        timeout=2,
    )

    assert not report.qualified
    call_counts = {case: sum(call["case"] == case for call in gateway.calls) for case in _CASES}
    assert call_counts["exact_once"] == 2
    assert all(count <= 2 for count in call_counts.values())


@pytest.mark.asyncio
async def test_cancellation_closes_the_synthetic_stream() -> None:
    gateway = _SyntheticGateway()
    report = await _service()(gateway, model=_MODEL, route_revision=_ROUTE)

    assert _case(report, "cancellation").passed
    assert "cancellation" in gateway.early_closed


@pytest.mark.asyncio
async def test_provider_error_reason_does_not_leak_secret_message() -> None:
    report = await _service()(
        _SyntheticGateway("secret_error"), model=_MODEL, route_revision=_ROUTE
    )

    assert not report.qualified
    assert all(_LEAK not in (case.reason or "") for case in report.cases)


@pytest.mark.asyncio
async def test_timeout_is_a_failed_report_and_closes_the_stream() -> None:
    gateway = _SyntheticGateway("timeout")
    report = await asyncio.wait_for(
        _service()(gateway, model=_MODEL, route_revision=_ROUTE, timeout_seconds=0.01),
        timeout=1,
    )

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
