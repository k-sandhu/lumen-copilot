"""Canonical corpus schema costs participate in the bounded chat context."""

# Imported fixtures intentionally share names with injected test parameters.
# ruff: noqa: F811

from __future__ import annotations

import asyncio
import sys
import types
import uuid
from collections.abc import AsyncIterator

import pytest

from tests.test_chat_runtime import _Ctx as _Ctx
from tests.test_chat_runtime import _drain as _drain
from tests.test_chat_runtime import _FakeRetrieval as _FakeRetrieval
from tests.test_chat_runtime import _runtime as _runtime
from tests.test_chat_runtime import ctx as ctx


async def test_canonical_search_stays_within_budget_and_refuses_unfit_evidence(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.domain.llm import ChatMessage, Role, StreamEvent, ToolCall
    from app.domain.retrieval import RetrievedPassage
    from app.llm.context import ContextConfig, estimate_message_tokens
    from app.realtime.backplane import InMemoryBackplane
    from app.services.prompts.grounded_answer import GROUNDED_SYSTEM_PROMPT
    from app.services.tools.registry import default_allowlist, tool_specs

    def bytecounter(text: str) -> int:
        return len(text.encode("utf-8"))

    def unavailable(**_kwargs: object) -> int:
        raise RuntimeError("no tokenizer or context-window metadata in this test")

    fake_litellm = types.SimpleNamespace(token_counter=unavailable, get_model_info=unavailable)
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)  # type: ignore[arg-type]

    fixed_messages = [
        ChatMessage(role=Role.SYSTEM, content=GROUNDED_SYSTEM_PROMPT),
        ChatMessage(role=Role.USER, content="fact"),
    ]
    fixed_cost = estimate_message_tokens(
        fixed_messages,
        tool_specs(default_allowlist()),
        counter=bytecounter,
    )
    budget = fixed_cost + 2_000

    class RecordingGateway:
        def __init__(self) -> None:
            self.estimates: list[int] = []
            self.calls = 0
            self.policy = ""
            self.offered_names: set[str] = set()

        async def stream_tools(
            self,
            messages: object,
            *,
            tools: object,
            **_kwargs: object,
        ) -> AsyncIterator[StreamEvent]:
            self.calls += 1
            assert isinstance(messages, list)
            assert isinstance(tools, list)
            self.policy = "\n".join(
                message.content or "" for message in messages if message.role == Role.SYSTEM
            )
            self.offered_names = {tool.name for tool in tools}
            self.estimates.append(estimate_message_tokens(messages, tools, counter=bytecounter))
            if self.calls == 1:
                yield StreamEvent(
                    tool_calls=(
                        ToolCall(
                            id="canonical-budget",
                            name="search_passages",
                            arguments={"query": "fact"},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            else:
                yield StreamEvent(finish_reason="stop")

    huge_passage = RetrievedPassage(
        chunk_id=ctx.chunk_id,
        document_id=ctx.document_id,
        document_name="taxes.pdf",
        ord=0,
        text="P" * 32_000,
        char_start=0,
        char_end=32_000,
        score=0.9,
    )
    retrieval = _FakeRetrieval([huge_passage])
    gateway = RecordingGateway()
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=retrieval,
        backplane=backplane,
        context_config=ContextConfig(
            fallback_max_input_tokens=budget + 1_024,
            output_headroom_tokens=0,
        ),
    )
    monkeypatch.setattr(runtime, "_token_counter_for", lambda _model: bytecounter)
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="fact",
        model="some/unknown-model-not-in-map",
        history=[],
        collection_ids=None,
    )
    events = await asyncio.wait_for(consumer, timeout=5.0)

    canonical = {"search_passages", "find_documents", "read_document"}
    assert canonical <= gateway.offered_names
    missing = {name for name in canonical if name not in gateway.policy}
    assert not missing, f"System policy omits offered corpus tools: {sorted(missing)}"
    assert gateway.calls == 1
    assert gateway.estimates
    assert all(estimate <= budget for estimate in gateway.estimates), gateway.estimates
    terminal = events[-1]
    assert terminal["type"] == "error"
    assert terminal["problem"]["code"] == "context_too_large"  # type: ignore[index]
