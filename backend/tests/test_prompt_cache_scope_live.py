"""Bounded #763 provider proofs; run alone in a fresh process with RUN_LIVE=1.

Only synthetic text is sent. Production settings/model defaults are not changed.
Generation identifiers and vendor diagnostics must remain private.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import litellm
import pytest

from app.core.config import Settings
from app.domain.llm import ChatMessage, Role, StreamEvent, TokenUsage, ToolSpec
from app.llm import LLMGateway, aclose_litellm_clients
from app.llm.gateway import _aclose

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE") != "1" or not os.environ.get("OPENROUTER_API_KEY", "").strip(),
        reason="requires RUN_LIVE=1 and a provider key",
    ),
]


async def _turn(
    gateway: LLMGateway, messages: list[ChatMessage], **kwargs: Any
) -> tuple[list[StreamEvent], TokenUsage]:
    events = [event async for event in gateway.stream_tools(messages, **kwargs)]
    usages = [event.usage for event in events if event.usage is not None]
    assert usages, "provider did not return token accounting"
    return events, usages[-1]


async def test_live_new_session_cache_reuses_only_same_user() -> None:
    settings = Settings()
    gateway = LLMGateway(settings)
    tenant, user, other_user = uuid4(), uuid4(), uuid4()
    # Fresh synthetic prefix avoids accidental warmth from an earlier probe.
    prefix = (
        "Synthetic cache proof "
        + uuid4().hex
        + "\n"
        + "\n".join(
            f"Reference {i}: all fictional values are local examples, never enterprise records."
            for i in range(320)
        )
    )

    def opening(question: str) -> list[ChatMessage]:
        return [
            ChatMessage(role=Role.SYSTEM, content=prefix + "\nReply with one word only."),
            ChatMessage(role=Role.USER, content=question),
        ]

    async def call(who: Any, question: str) -> TokenUsage:
        _, usage = await _turn(
            gateway,
            opening(question),
            tools=(),
            tool_choice="none",
            model="openrouter/openai/gpt-6-luna",
            max_tokens=32,
            tenant_id=tenant,
            user_id=who,
            session_id=uuid4(),
        )
        return usage

    try:
        first = await call(user, "Say alpha.")
        second = await call(user, "Say beta.")
        different = await call(other_user, "Say beta.")
        print(
            {
                "proof": "cache_partition",
                "first_prompt_tokens": first.prompt_tokens,
                "second_cached_tokens": second.cached_prompt_tokens,
                "different_user_cached_tokens": different.cached_prompt_tokens,
            }
        )
        assert second.cached_prompt_tokens > 0, "second session did not reuse this user's prefix"
        assert (
            different.cached_prompt_tokens == 0
        ), "different user read another user's cache partition"
    finally:
        # This module is an isolated process-only live suite: no concurrent app loops.
        await aclose_litellm_clients()


class _ObservedStream:
    def __init__(self, inner: Any, generation_ids: list[str]) -> None:
        self.inner, self.generation_ids = inner, generation_ids

    async def __aiter__(self) -> AsyncIterator[Any]:
        recorded = False
        async for part in self.inner:
            identifier = getattr(part, "id", None)
            if not recorded and isinstance(identifier, str) and identifier:
                self.generation_ids.append(identifier)
                recorded = True
            yield part

    async def aclose(self) -> None:
        await _aclose(self.inner)


async def _provider(client: httpx.AsyncClient, identifier: str) -> str:
    # Completion records can become visible shortly after the stream finishes.
    for attempt in range(10):
        response = await client.get(
            "https://openrouter.ai/api/v1/generation", params={"id": identifier}
        )
        if response.status_code == 200:
            name = response.json().get("data", {}).get("provider_name")
            assert isinstance(name, str) and name, "generation record lacks serving provider"
            return name
        assert response.status_code == 404, "generation lookup failed (status only)"
        if attempt < 9:
            await asyncio.sleep(0.5)
    raise AssertionError("generation record not available within bounded lookup window")


async def test_live_multicall_answer_keeps_serving_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    gateway = LLMGateway(settings)
    generations: list[str] = []
    real_completion = litellm.acompletion

    async def observed(**kwargs: Any) -> _ObservedStream:
        return _ObservedStream(await real_completion(**kwargs), generations)

    monkeypatch.setattr(litellm, "acompletion", observed)
    ids = {"tenant_id": uuid4(), "user_id": uuid4(), "session_id": uuid4()}
    messages = [
        ChatMessage(
            role=Role.SYSTEM,
            content=(
                "Synthetic lookup exercise. Use lookup when required, "
                "then answer tersely from its fictional results."
            ),
        ),
        ChatMessage(role=Role.USER, content="Look up two fictional values, then summarize them."),
    ]
    tools = (
        ToolSpec(
            name="lookup",
            description="Read a fictional fixture value",
            parameters={"type": "object", "properties": {"key": {"type": "string"}}},
        ),
    )
    try:
        for step in range(2):
            events, _ = await _turn(
                gateway,
                messages,
                tools=tools,
                tool_choice="required",
                model="openrouter/deepseek/deepseek-v4.1-flash",
                max_tokens=128,
                **ids,
            )
            calls = [call for event in events for call in event.tool_calls]
            assert calls and all(
                call.name == "lookup" for call in calls
            ), "required synthetic lookup not produced"
            messages.append(
                ChatMessage(
                    role=Role.ASSISTANT,
                    content="".join(event.text for event in events),
                    tool_calls=tuple(calls),
                )
            )
            messages.extend(
                ChatMessage(
                    role=Role.TOOL,
                    content=f"Fictional value {step}: {step+1}.",
                    tool_call_id=call.id,
                    name=call.name,
                )
                for call in calls
            )
        events, _ = await _turn(
            gateway,
            messages,
            tools=tools,
            tool_choice="none",
            model="openrouter/deepseek/deepseek-v4.1-flash",
            max_tokens=128,
            **ids,
        )
        assert any(event.text for event in events), "forced synthesis returned no text"
        assert len(generations) == 3, "missing generation evidence for a model call"
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + settings.openrouter_api_key}, timeout=10
        ) as client:
            providers = [await _provider(client, identifier) for identifier in generations]
        print(
            {
                "proof": "conversation_affinity",
                "model_calls": len(generations),
                "distinct_serving_providers": len(set(providers)),
            }
        )
        assert len(set(providers)) == 1, "one multicall answer changed serving provider"
    finally:
        await aclose_litellm_clients()
