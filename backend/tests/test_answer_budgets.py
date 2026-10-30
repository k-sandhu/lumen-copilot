"""Shared answer budgets + stream cancel registry (#574).

Offline unit tests: no DB, no Redis. Covers the budget defaults/resolution,
the external-call classifier, the backplane cancel signal, and the
stream-keyed task registry that replaces the unkeyed answer-task set.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.realtime.backplane import InMemoryBackplane, is_terminal
from app.services.answer_budgets import AnswerBudgets, budgets_from_settings, is_external_tool
from app.services.stream_registry import StreamRegistry


def test_budgets_defaults_match_live_chat_values() -> None:
    budgets = budgets_from_settings(Settings())
    settings = Settings()
    assert budgets.max_tool_turns == settings.chat_max_tool_turns == 20
    assert budgets.turn_timeout_seconds == settings.llm_interactive_timeout_seconds
    assert budgets.interactive_max_attempts == settings.llm_interactive_max_attempts
    assert budgets.answer_max_tokens == settings.chat_answer_max_tokens
    assert budgets.wall_clock_seconds == settings.chat_answer_wall_clock_seconds
    assert budgets.max_external_calls == settings.chat_max_external_calls_per_answer


def test_budgets_tenant_override_beats_default_tool_turns() -> None:
    budgets = AnswerBudgets(max_tool_turns=20)
    assert budgets.effective_max_tool_turns(tenant_override=3) == 3
    assert budgets.effective_max_tool_turns(tenant_override=None) == 20
    assert budgets.effective_max_tool_turns(tenant_override=0) == 1


def test_budgets_are_immutable() -> None:
    budgets = AnswerBudgets()
    with pytest.raises(FrozenInstanceError):
        budgets.max_tool_turns = 99  # type: ignore[misc]


def test_external_tool_classifier() -> None:
    assert is_external_tool("web_search")
    assert is_external_tool("web_fetch")
    assert is_external_tool("mcp:server:tool")
    assert not is_external_tool("search_text")
    assert not is_external_tool("search_passages")
    assert not is_external_tool("run_python")


async def test_inmemory_backplane_cancel_signal_roundtrip() -> None:
    backplane = InMemoryBackplane()
    assert await backplane.is_cancelled("s1") is False
    await backplane.signal_cancel("s1")
    assert await backplane.is_cancelled("s1") is True
    # Idempotent: signalling twice stays cancelled, other streams unaffected.
    await backplane.signal_cancel("s1")
    assert await backplane.is_cancelled("s1") is True
    assert await backplane.is_cancelled("s2") is False


def test_cancelled_is_a_terminal_envelope() -> None:
    assert is_terminal({"type": "cancelled"})
    assert is_terminal({"type": "done"})
    assert not is_terminal({"type": "delta"})


async def test_registry_cancels_registered_task_and_runtime() -> None:
    registry = StreamRegistry()
    started = asyncio.Event()
    release = asyncio.Event()

    class _Runtime:
        cancelled = False

        def request_cancel(self) -> None:
            self.cancelled = True

    runtime = _Runtime()

    async def _produce() -> None:
        started.set()
        await release.wait()

    task = asyncio.create_task(_produce())
    await started.wait()
    registry.register("stream-1", task=task, runtime=runtime)  # type: ignore[arg-type]

    assert registry.cancel("stream-1") is True
    assert runtime.cancelled is True
    with pytest.raises(asyncio.CancelledError):
        await task
    # Repeated cancel is safe; unknown stream reports False.
    assert registry.cancel("stream-1") is False
    assert registry.cancel("nope") is False
    assert registry.is_active("stream-1") is False


async def test_registry_reports_completed_stream() -> None:
    registry = StreamRegistry()

    async def _done() -> None:
        return None

    task = asyncio.create_task(_done())
    await task
    registry.register("stream-2", task=task, runtime=None)
    assert registry.is_active("stream-2") is False
    assert registry.cancel("stream-2") is False
    pruned = asyncio.Event()
    task.add_done_callback(lambda _task: pruned.set())
    await pruned.wait()
    assert registry.known("stream-2") is False
    assert registry.tasks() == []
    assert registry.known(uuid.uuid4().hex) is False


def test_research_ceiling_defaults_and_settings_are_shared() -> None:
    budgets = budgets_from_settings(Settings())
    assert budgets.max_tool_calls == 60
    assert budgets.max_calls_per_round == 8
    assert budgets.max_input_tokens == 500_000
    assert budgets.max_output_tokens == 16_000
    assert budgets.max_cost_usd is None
    tuned = budgets_from_settings(
        Settings(
            CHAT_MAX_TOOL_CALLS_PER_ANSWER=7,
            CHAT_MAX_CALLS_PER_ROUND=2,
            CHAT_MAX_INPUT_TOKENS_PER_ANSWER=300,
            CHAT_MAX_OUTPUT_TOKENS_PER_ANSWER=40,
            CHAT_MAX_COST_USD_PER_ANSWER="0.004",
        )
    )
    assert (
        tuned.max_tool_calls,
        tuned.max_calls_per_round,
        tuned.max_input_tokens,
        tuned.max_output_tokens,
        tuned.max_cost_usd,
    ) == (7, 2, 300, 40, Decimal("0.004"))


@pytest.mark.parametrize(
    "field", ["max_tool_calls", "max_input_tokens", "max_output_tokens", "max_calls_per_round"]
)
@pytest.mark.parametrize("value", [-1, True, float("nan"), float("inf")])
def test_direct_ceiling_values_refuse_invalid_numbers(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        AnswerBudgets(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), True])
def test_cost_ceiling_refuses_invalid_numbers(value: object) -> None:
    with pytest.raises(ValueError):
        AnswerBudgets(max_cost_usd=value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    [
        "CHAT_MAX_TOOL_CALLS_PER_ANSWER",
        "CHAT_MAX_CALLS_PER_ROUND",
        "CHAT_MAX_INPUT_TOKENS_PER_ANSWER",
        "CHAT_MAX_OUTPUT_TOKENS_PER_ANSWER",
        "CHAT_MAX_COST_USD_PER_ANSWER",
    ],
)
@pytest.mark.parametrize("value", [-1, True, "NaN", "Infinity"])
def test_settings_ceiling_values_fail_at_configuration_boundary(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        Settings(**{field: value})


def test_numeric_environment_overrides_reach_every_shared_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "CHAT_MAX_TOOL_CALLS_PER_ANSWER": "9",
        "CHAT_MAX_CALLS_PER_ROUND": "3",
        "CHAT_MAX_INPUT_TOKENS_PER_ANSWER": "6000",
        "CHAT_MAX_OUTPUT_TOKENS_PER_ANSWER": "120",
        "CHAT_MAX_COST_USD_PER_ANSWER": "0.003",
    }.items():
        monkeypatch.setenv(name, value)
    budgets = budgets_from_settings(Settings())
    assert (budgets.max_tool_calls, budgets.max_calls_per_round) == (9, 3)
    assert (budgets.max_input_tokens, budgets.max_output_tokens) == (6000, 120)
    assert budgets.max_cost_usd == Decimal("0.003")
