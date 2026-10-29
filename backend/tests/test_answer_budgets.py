"""Shared answer budgets + stream cancel registry (#574).

Offline unit tests: no DB, no Redis. Covers the budget defaults/resolution,
the external-call classifier, the backplane cancel signal, and the
stream-keyed task registry that replaces the unkeyed answer-task set.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import FrozenInstanceError

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
