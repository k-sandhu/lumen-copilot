"""Deterministic hostile-model ceilings and disclosures (#833, spec 0028)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.api.v1.chat import _message_to_response
from app.db import models
from app.db.repositories import MessageRepository
from app.domain.llm import ChatMessage, StreamEvent, TokenUsage, ToolCall, ToolSpec
from app.domain.tools import ToolHandlerResult
from app.llm import LlmProviderError
from app.llm.calls import mark_dispatched
from app.llm.context import ContextConfig, estimate_message_tokens
from app.realtime.backplane import InMemoryBackplane
from app.services import chat_runtime as runtime_module
from app.services.answer_budgets import AnswerBudgets
from app.services.assistant_runtime import AssistantRunConfig
from app.services.chat_runtime import ChatRuntime
from app.services.chat_service import MessageView
from app.services.tools.types import ToolContext, ToolDefinition
from tests.test_chat_runtime import _Ctx, _FakeRetrieval
from tests.test_chat_runtime import ctx as _runtime_ctx

ctx = _runtime_ctx


def _count(text: str) -> int:
    return len(text)


class _HostileGateway:
    """Offers 100 rounds of 40 unique calls, without ever choosing to finish."""

    def __init__(
        self,
        *,
        per_round: int = 40,
        output: int = 20,
        cost: Decimal | None = Decimal("0.04"),
        normal_after: int | None = None,
        names: tuple[str, ...] = ("mcp:test:lookup",),
        missing_usage: bool = False,
        synthesis_finish: str = "stop",
        request_ceiling: int = 100,
        cost_only_usage: bool = False,
    ) -> None:
        self.per_round = per_round
        self.output = output
        self.cost = cost
        self.normal_after = normal_after
        self.names = names
        self.missing_usage = missing_usage
        self.synthesis_finish = synthesis_finish
        self.request_ceiling = request_ceiling
        self.cost_only_usage = cost_only_usage
        self.requests: list[dict[str, Any]] = []
        self.synthesis: list[dict[str, Any]] = []

    async def stream_tools(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        model: str,
        tool_choice: str | None = None,
        max_tokens: int | None = None,
        **_kwargs: object,
    ) -> AsyncIterator[StreamEvent]:
        projected = estimate_message_tokens(messages, tools, counter=_count)
        record = {"messages": list(messages), "projected": projected, "max_tokens": max_tokens}
        if tool_choice == "none":
            self.synthesis.append(record)
            yield StreamEvent(text="Answer from the available evidence.")
            yield StreamEvent(
                finish_reason=self.synthesis_finish,
                usage=TokenUsage(
                    prompt_tokens=projected,
                    completion_tokens=10,
                    total_tokens=projected + 10,
                    cost_usd=Decimal("0.01"),
                ),
            )
            return
        self.requests.append(record)
        assert (
            len(self.requests) <= self.request_ceiling
        ), "hostile model unexpectedly escaped the answer bounds"
        output = min(self.output, max_tokens) if max_tokens is not None else self.output
        usage = (
            None
            if self.missing_usage
            else TokenUsage(
                prompt_tokens=projected,
                completion_tokens=output,
                total_tokens=projected + output,
                cost_usd=self.cost,
            )
        )
        if self.cost_only_usage:
            usage = TokenUsage(cost_usd=self.cost)
        if self.normal_after is not None and len(self.requests) > self.normal_after:
            yield StreamEvent(text="An ordinary answer.")
            yield StreamEvent(finish_reason="stop", usage=usage)
            return
        round_index = len(self.requests)
        yield StreamEvent(
            tool_calls=tuple(
                ToolCall(
                    id=f"r{round_index}-c{i}",
                    name=self.names[i % len(self.names)],
                    arguments=(
                        {"index": i, "query": "x" * 100}
                        if self.names[i % len(self.names)].startswith("mcp:")
                        else {"query": "x" * 100}
                    ),
                )
                for i in range(self.per_round)
            ),
            finish_reason="tool_calls",
            usage=usage,
        )


async def _answer(
    ctx: _Ctx,
    gateway: _HostileGateway,
    budgets: AnswerBudgets,
    *,
    expect_ok: bool = True,
    retrieval: _FakeRetrieval | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], ChatRuntime]:
    executed: list[dict[str, Any]] = []
    retrieval = retrieval or _FakeRetrieval([])

    async def retry_sleep(_seconds: float) -> None:
        return None

    async def lookup(args: dict[str, Any], _context: ToolContext) -> ToolHandlerResult:
        executed.append(args)
        return ToolHandlerResult(content="Evidence " + "p" * 1000, source_document_ids=())

    definition = ToolDefinition(
        name="mcp:test:lookup",
        description="Find permitted evidence.",
        json_schema={
            "type": "object",
            "properties": {"index": {"type": "integer"}, "query": {"type": "string"}},
        },
        handler=lookup,
    )

    async def factory(_session: object) -> dict[str, ToolDefinition]:
        return {definition.name: definition}

    backplane = InMemoryBackplane()
    runtime = ChatRuntime(
        sessionmaker=ctx.sessionmaker,
        gateway=gateway,  # type: ignore[arg-type]
        backplane=backplane,
        principal=ctx.principal,
        request_id="answer-ceilings",
        source_ip="127.0.0.1",
        budgets=budgets,
        retrieval_factory=lambda _session: retrieval,  # type: ignore[arg-type,return-value]
        mcp_tools_factory=factory,  # type: ignore[arg-type]
        suggestions_enabled=False,
        retry_sleep=retry_sleep,
        context_config=ContextConfig(fallback_max_input_tokens=2_000_000, output_headroom_tokens=0),
    )
    runtime._token_counters["test/model"] = _count
    runtime._token_counters["test/fallback"] = _count
    stream_id = uuid.uuid4().hex
    result = await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q" * 2000,
        model="test/model",
        history=[],
        collection_ids=None,
        assistant_config=AssistantRunConfig(
            system_prompt="Use permitted evidence.",
            allowed=frozenset(gateway.names),
            collection_ids=None,
            model=None,
        ),
    )
    assert result is expect_ok
    envs = [env async for env in backplane.subscribe(stream_id)]
    return envs, executed, runtime


def _done(envs: list[dict[str, Any]]) -> dict[str, Any]:
    terminals = [env for env in envs if env["type"] in {"done", "error", "cancelled"}]
    assert [env["type"] for env in terminals] == ["done"]
    return terminals[0]["data"]


async def _assert_disclosed(ctx: _Ctx, envs: list[dict[str, Any]], kind: str) -> None:
    data = _done(envs)
    assert data["limit"]["kind"] == kind
    sentence = data["limit"]["message"]
    assert sentence.endswith(".") and not any(char.isdigit() for char in sentence)
    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
    assert messages[-1].limit_kind == kind
    assert str(messages[-1].id) == data["messageId"]
    reloaded = _message_to_response(MessageView(message=messages[-1], citations=[]))
    assert reloaded.model_dump(mode="json")["limit"] == data["limit"]


@pytest.fixture
def outcome_records(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    class _Logger:
        def info(self, event: str, **fields: Any) -> None:
            if event == "llm.answer_outcome":
                records.append(fields)

        warning = info
        debug = info
        error = info

    monkeypatch.setattr(runtime_module, "log", _Logger())
    return records


@pytest.mark.parametrize(
    ("kind", "changes", "requests"),
    [
        ("tool_turns", {"max_tool_turns": 2}, 2),
        ("tool_calls", {"max_tool_calls": 3}, 1),
        ("external_calls", {"max_external_calls": 3}, 1),
        ("input_tokens", {"max_input_tokens": 3000}, 1),
        ("output_tokens", {"max_output_tokens": 25}, 2),
        ("cost", {"max_cost_usd": Decimal("0.05")}, 2),
    ],
)
async def test_each_ceiling_stops_before_next_provider_request_and_discloses(
    ctx: _Ctx,
    kind: str,
    changes: dict[str, Any],
    requests: int,
    outcome_records: list[dict[str, Any]],
) -> None:
    gateway = _HostileGateway(request_ceiling=requests)
    budgets = replace(
        AnswerBudgets(
            max_tool_turns=100,
            max_tool_calls=1000,
            max_calls_per_round=8,
            max_model_calls=101,
            max_input_tokens=2_000_000,
            max_output_tokens=16000,
        ),
        **changes,
    )
    envs, executed, runtime = await _answer(ctx, gateway, budgets)
    await _assert_disclosed(ctx, envs, kind)
    assert len(gateway.requests) == requests
    assert len(gateway.synthesis) == 1
    assert runtime._model_calls == requests + 1
    assert len(outcome_records) == 1
    assert outcome_records[0]["limit_kind"] == kind
    assert outcome_records[0]["outcome"] == "budget_exhausted"
    assert "q" * 2000 not in str(outcome_records)
    assert len(executed) <= budgets.max_tool_calls
    if kind == "tool_calls":
        assert len(executed) == 3
    if kind == "external_calls":
        assert len(executed) == 3
    if kind == "output_tokens":
        assert [request["max_tokens"] for request in gateway.requests] == [25, 5]
    if kind == "cost":
        assert Decimal("0.08") - budgets.max_cost_usd <= gateway.cost


async def test_per_round_skips_pair_results_and_are_audited_without_execution(ctx: _Ctx) -> None:
    gateway = _HostileGateway(normal_after=1)
    envs, executed, _runtime = await _answer(ctx, gateway, AnswerBudgets(max_calls_per_round=2))
    assert len(executed) == 2
    assert "limit" not in _done(envs)
    assert gateway.synthesis == []
    tool_messages = [m for m in gateway.requests[1]["messages"] if m.role.value == "tool"]
    assert len(tool_messages) == 40
    assert len({m.tool_call_id for m in tool_messages}) == 40
    skipped = [m for m in tool_messages if "next round" in m.content]
    assert len(skipped) == 38
    results = [env["data"] for env in envs if env.get("name") == "tool_result"]
    assert len(results) == 40 and sum(result["ok"] for result in results) == 2
    async with ctx.sessionmaker() as session:
        invocations = list((await session.scalars(select(models.ToolInvocation))).all())
        audits = list(
            (
                await session.scalars(
                    select(models.AuditEvent).where(
                        models.AuditEvent.action.in_(["tool.invoked", "tool.result"])
                    )
                )
            ).all()
        )
    assert len(invocations) == 40
    denied = [row for row in invocations if not row.ok]
    assert len(denied) == 38
    assert all(row.error == "answer_budget_exhausted" for row in denied)
    assert len(audits) == 80
    assert sum(row.outcome == "denied" for row in audits) == 76
    assert {row.event_metadata["denied_reason"] for row in audits if row.outcome == "denied"} == {
        "calls_per_round"
    }


async def test_unpriced_cost_ceiling_stops_and_reports_unknown_cost(
    ctx: _Ctx,
    outcome_records: list[dict[str, Any]],
) -> None:
    gateway = _HostileGateway(cost=None)
    envs, _executed, _runtime = await _answer(
        ctx, gateway, AnswerBudgets(max_cost_usd=Decimal("1"))
    )
    await _assert_disclosed(ctx, envs, "cost")
    assert len(gateway.requests) == 1 and len(gateway.synthesis) == 1
    assert _done(envs)["usage"]["unpricedCalls"] == 1
    assert outcome_records[0]["limit_kind"] == "cost"
    assert outcome_records[0]["unpriced_calls"] == 1


async def test_forced_synthesis_is_single_request_even_when_length_capped(ctx: _Ctx) -> None:
    gateway = _HostileGateway(synthesis_finish="length")
    envs, _executed, _runtime = await _answer(
        ctx, gateway, AnswerBudgets(max_tool_calls=1, answer_max_tokens=31)
    )
    await _assert_disclosed(ctx, envs, "tool_calls")
    assert len(gateway.requests) == 1 and len(gateway.synthesis) == 1
    assert gateway.synthesis[0]["max_tokens"] == 31


async def test_adversarial_hundred_by_forty_stays_inside_all_bounds(ctx: _Ctx) -> None:
    gateway = _HostileGateway()
    budgets = AnswerBudgets(
        max_tool_turns=100,
        max_tool_calls=13,
        max_calls_per_round=3,
        max_external_calls=11,
        max_model_calls=7,
        max_input_tokens=500_000,
        max_output_tokens=85,
        max_cost_usd=Decimal("0.13"),
        answer_max_tokens=50,
    )
    envs, executed, runtime = await _answer(ctx, gateway, budgets)
    assert len(executed) <= 11
    assert sum(request["projected"] for request in gateway.requests) <= budgets.max_input_tokens
    assert (
        sum(min(gateway.output, request["max_tokens"]) for request in gateway.requests)
        <= budgets.max_output_tokens
    )
    assert len(gateway.requests) * gateway.cost <= budgets.max_cost_usd + gateway.cost
    assert runtime._model_calls <= budgets.max_model_calls
    assert len(gateway.synthesis) == 1
    assert _done(envs)["limit"]["kind"] in {
        "tool_calls",
        "external_calls",
        "input_tokens",
        "output_tokens",
        "cost",
    }
    for round_index in range(1, len(gateway.requests) + 1):
        call_results = [
            env["data"]
            for env in envs
            if env.get("name") == "tool_result"
            and env["data"]["callId"].startswith(f"r{round_index}-")
        ]
        assert len(call_results) == 40
        assert sum(result["ok"] for result in call_results) <= budgets.max_calls_per_round


async def test_normal_done_omits_limit_and_persisted_kind_is_null(ctx: _Ctx) -> None:
    gateway = _HostileGateway(normal_after=0)
    envs, executed, _runtime = await _answer(ctx, gateway, AnswerBudgets())
    assert not executed and not gateway.synthesis
    data = _done(envs)
    assert set(data) == {"messageId", "finishReason", "model", "citationCount", "usage"}
    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
    assert messages[-1].limit_kind is None


async def test_partial_external_admission_keeps_later_internal_calls(ctx: _Ctx) -> None:
    gateway = _HostileGateway(
        per_round=4, names=("mcp:test:lookup", "mcp:test:lookup", "search_text", "search_text")
    )
    retrieval = _FakeRetrieval([])
    envs, executed, _runtime = await _answer(
        ctx,
        gateway,
        AnswerBudgets(max_external_calls=1, max_tool_calls=3, max_calls_per_round=3),
        retrieval=retrieval,
    )
    assert len(executed) == 1
    assert len(retrieval.queries) == 2
    assert len(gateway.requests) == 1 and len(gateway.synthesis) == 1
    assert _done(envs)["limit"]["kind"] == "tool_calls"
    results = [env["data"] for env in envs if env.get("name") == "tool_result"]
    assert len(results) == 4
    assert {result["callId"]: result["ok"] for result in results} == {
        "r1-c0": True,
        "r1-c1": False,
        "r1-c2": True,
        "r1-c3": True,
    }
    replies = [m for m in gateway.synthesis[0]["messages"] if m.role.value == "tool"]
    assert len(replies) == 4
    assert "Do not retry" in replies[1].content


async def test_no_retry_advice_when_the_last_round_ends_research(ctx: _Ctx) -> None:
    gateway = _HostileGateway(per_round=3)
    envs, executed, _runtime = await _answer(
        ctx, gateway, AnswerBudgets(max_tool_turns=1, max_calls_per_round=1)
    )
    assert len(executed) == 1
    await _assert_disclosed(ctx, envs, "tool_turns")
    replies = [m for m in gateway.synthesis[0]["messages"] if m.role.value == "tool"]
    assert len(replies) == 3
    assert all("Do not retry" in reply.content for reply in replies[1:])


class _FailOnceGateway(_HostileGateway):
    async def stream_tools(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        tool_choice: str | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        if tool_choice != "none" and not self.requests:
            projected = estimate_message_tokens(messages, tools, counter=_count)
            self.requests.append(
                {"messages": list(messages), "projected": projected, "max_tokens": max_tokens}
            )
            mark_dispatched()
            raise LlmProviderError(
                "Transient provider failure.", retryable=True, code="provider_unavailable"
            )
        async for event in super().stream_tools(
            messages, tools=tools, tool_choice=tool_choice, max_tokens=max_tokens, **kwargs
        ):
            yield event


async def test_failed_attempt_reserves_output_before_retry(ctx: _Ctx) -> None:
    gateway = _FailOnceGateway()
    envs, _executed, runtime = await _answer(
        ctx, gateway, AnswerBudgets(max_output_tokens=50, answer_max_tokens=50)
    )
    await _assert_disclosed(ctx, envs, "output_tokens")
    assert len(gateway.requests) == 1 and len(gateway.synthesis) == 1
    assert runtime._model_calls == 2
    assert _done(envs)["usage"]["unpricedCalls"] == 1


async def test_cost_unknown_failed_attempt_prevents_retry(ctx: _Ctx) -> None:
    gateway = _FailOnceGateway()
    envs, _executed, _runtime = await _answer(
        ctx, gateway, AnswerBudgets(max_cost_usd=Decimal("2"))
    )
    await _assert_disclosed(ctx, envs, "cost")
    assert len(gateway.requests) == 1 and len(gateway.synthesis) == 1


async def test_missing_usage_reserves_the_requested_output(ctx: _Ctx) -> None:
    gateway = _HostileGateway(missing_usage=True)
    envs, _executed, runtime = await _answer(
        ctx, gateway, AnswerBudgets(max_output_tokens=50, answer_max_tokens=50)
    )
    await _assert_disclosed(ctx, envs, "output_tokens")
    assert len(gateway.requests) == 1 and len(gateway.synthesis) == 1
    assert runtime._loop_output_tokens == 50


async def test_model_attempt_cap_remains_absolute_when_synthesis_is_needed(ctx: _Ctx) -> None:
    gateway = _HostileGateway(per_round=1)
    envs, _executed, runtime = await _answer(
        ctx, gateway, AnswerBudgets(max_tool_turns=1, max_model_calls=1), expect_ok=False
    )
    assert len(gateway.requests) == 1 and len(gateway.synthesis) == 0
    assert runtime._model_calls == 1
    terminal = [env for env in envs if env["type"] == "error"]
    assert len(terminal) == 1
    assert terminal[0]["problem"]["code"] == "answer_budget_exhausted"


async def test_expired_answer_deadline_stops_before_any_provider_request(
    ctx: _Ctx,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    monkeypatch.setattr(
        ChatRuntime,
        "_answer_deadline_at",
        lambda _self, _seconds: asyncio.get_running_loop().time() - 1,
    )
    gateway = _HostileGateway(request_ceiling=0)
    envs, _executed, runtime = await _answer(
        ctx, gateway, AnswerBudgets(wall_clock_seconds=600), expect_ok=False
    )
    assert gateway.requests == [] and gateway.synthesis == []
    assert runtime._model_calls == 0
    terminal = [env for env in envs if env["type"] == "error"]
    assert len(terminal) == 1
    assert terminal[0]["problem"]["code"] == "answer_budget_exhausted"


async def test_budget_refusal_audit_failure_aborts_the_answer(
    ctx: _Ctx,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.domain.audit import AuditAction
    from app.services.audit import AuditSink

    original = AuditSink.emit

    async def fail_budget_audit(self: AuditSink, **kwargs: Any) -> Any:
        if (
            kwargs.get("action") == AuditAction.TOOL_INVOKED
            and kwargs.get("metadata", {}).get("denied_reason") == "calls_per_round"
        ):
            raise RuntimeError("Audit persistence is unavailable")
        return await original(self, **kwargs)

    monkeypatch.setattr(AuditSink, "emit", fail_budget_audit)
    gateway = _HostileGateway(per_round=2)
    envs, executed, _runtime = await _answer(
        ctx, gateway, AnswerBudgets(max_calls_per_round=1), expect_ok=False
    )
    assert len(executed) == 1
    assert len(gateway.requests) == 1 and gateway.synthesis == []
    assert [env["type"] for env in envs if env["type"] in {"done", "error"}] == ["error"]
    async with ctx.sessionmaker() as session:
        assert (
            await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id) == []
        )
        assert list((await session.scalars(select(models.ToolInvocation))).all()) == []


@pytest.mark.parametrize(
    ("kind", "changes"),
    [
        ("tool_calls", {"max_tool_calls": 0}),
        ("external_calls", {"max_external_calls": 0}),
        ("input_tokens", {"max_input_tokens": 0}),
        ("output_tokens", {"max_output_tokens": 0}),
        ("cost", {"max_cost_usd": Decimal(0)}),
    ],
)
async def test_zero_allowance_synthesizes_without_starting_research(
    ctx: _Ctx,
    kind: str,
    changes: dict[str, Any],
) -> None:
    gateway = _HostileGateway(request_ceiling=0)
    envs, executed, _runtime = await _answer(ctx, gateway, replace(AnswerBudgets(), **changes))
    assert gateway.requests == [] and executed == []
    assert len(gateway.synthesis) == 1
    await _assert_disclosed(ctx, envs, kind)


class _PartiallyReportedFailureGateway(_HostileGateway):
    async def stream_tools(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        tool_choice: str | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        if tool_choice != "none":
            projected = estimate_message_tokens(messages, tools, counter=_count)
            self.requests.append(
                {"messages": list(messages), "projected": projected, "max_tokens": max_tokens}
            )
            assert len(self.requests) == 1, "a reported prompt spend must survive a failed attempt"
            mark_dispatched()
            yield StreamEvent(
                usage=TokenUsage(
                    prompt_tokens=50_000,
                    completion_tokens=1,
                    total_tokens=50_001,
                    cost_usd=Decimal("0.01"),
                )
            )
            raise LlmProviderError("Transient failure after usage.", retryable=True)
        async for event in super().stream_tools(
            messages, tools=tools, tool_choice=tool_choice, max_tokens=max_tokens, **kwargs
        ):
            yield event


async def test_partial_usage_of_a_failed_request_counts_before_retry(ctx: _Ctx) -> None:
    gateway = _PartiallyReportedFailureGateway()
    envs, _executed, runtime = await _answer(ctx, gateway, AnswerBudgets(max_input_tokens=50_000))
    await _assert_disclosed(ctx, envs, "input_tokens")
    assert len(gateway.requests) == 1 and len(gateway.synthesis) == 1
    assert runtime._loop_input_tokens == 50_000


async def test_cost_only_usage_retains_the_output_reservation(ctx: _Ctx) -> None:
    gateway = _HostileGateway(cost_only_usage=True, request_ceiling=1)
    envs, _executed, runtime = await _answer(
        ctx, gateway, AnswerBudgets(max_output_tokens=50, answer_max_tokens=50)
    )
    await _assert_disclosed(ctx, envs, "output_tokens")
    assert len(gateway.requests) == 1 and len(gateway.synthesis) == 1
    assert runtime._loop_output_tokens == 50
    assert _done(envs)["usage"]["unpricedCalls"] == 0
