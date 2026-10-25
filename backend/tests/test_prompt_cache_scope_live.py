"""Bounded #763 provider proofs; run alone in a fresh process with RUN_LIVE=1.

Only synthetic text is sent. Production settings/model defaults are not changed.
Generation identifiers and vendor diagnostics must remain private.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
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
    def __init__(self, inner: Any, generation_ids: list[str], private_path: Path) -> None:
        self.inner, self.generation_ids, self.private_path = inner, generation_ids, private_path

    async def __aiter__(self) -> AsyncIterator[Any]:
        index = len(self.generation_ids)
        self.generation_ids.append("")
        async for part in self.inner:
            identifier = getattr(part, "id", None)
            if isinstance(identifier, str) and identifier:
                # Prefer the router generation id over a temporary/upstream id.
                current = self.generation_ids[index]
                if identifier.startswith("gen-") or not current.startswith("gen-"):
                    self.generation_ids[index] = identifier
                    self.private_path.write_text(json.dumps(self.generation_ids), encoding="utf-8")
            yield part

    async def aclose(self) -> None:
        await _aclose(self.inner)


async def _provider(client: httpx.AsyncClient, identifier: str, deadline: float) -> str:
    # One common 60s window for all metadata, not a generation retry or model budget.
    loop = asyncio.get_running_loop()
    statuses: list[int] = []
    try:
        while loop.time() < deadline:
            remaining = deadline - loop.time()
            try:
                async with asyncio.timeout(min(5.0, remaining)):
                    response = await client.get(
                        "https://openrouter.ai/api/v1/generation", params={"id": identifier}
                    )
            except (TimeoutError, httpx.TimeoutException):
                continue
            statuses.append(response.status_code)
            if response.status_code == 200:
                name = response.json().get("data", {}).get("provider_name")
                assert isinstance(name, str) and name, "generation record lacks serving provider"
                return name
            assert response.status_code == 404, "generation lookup failed (status only)"
            await asyncio.sleep(min(2.0, max(0.0, deadline - loop.time())))
        raise AssertionError("generation record not available within bounded lookup window")
    finally:
        print({"proof": "generation_lookup", "statuses": statuses})


async def test_live_multicall_answer_keeps_serving_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings()
    gateway = LLMGateway(settings)
    generations: list[str] = []
    real_completion = litellm.acompletion

    async def observed(**kwargs: Any) -> _ObservedStream:
        return _ObservedStream(
            await real_completion(**kwargs), generations, tmp_path / "generations.json"
        )

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
    # A 128-token probe saturated with finish_reason=length and no tool call.
    # This synthetic-only allowance bounds reasoning plus output; production
    # model settings, retry/time budgets and every proof assertion stay unchanged.
    try:
        for step in range(2):
            events, usage = await _turn(
                gateway,
                messages,
                tools=tools,
                tool_choice="required",
                model="openrouter/deepseek/deepseek-v4.1-flash",
                max_tokens=1024,
                **ids,
            )
            calls = [call for event in events for call in event.tool_calls]
            print(
                {
                    "proof": "required_turn",
                    "step": step,
                    "finish_reasons": [
                        event.finish_reason for event in events if event.finish_reason
                    ],
                    "completion_tokens": usage.completion_tokens,
                    "tool_calls": len(calls),
                    "text_chunks": sum(bool(event.text) for event in events),
                }
            )
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
            max_tokens=1024,
            **ids,
        )
        assert any(event.text for event in events), "forced synthesis returned no text"
        assert len(generations) == 3, "missing generation evidence for a model call"
        print(
            {
                "proof": "generation_id_formats",
                "router_ids": sum(identifier.startswith("gen-") for identifier in generations),
                "upstream_ids": sum(
                    identifier.startswith("chatcmpl-") for identifier in generations
                ),
            }
        )
        assert all(
            identifier.startswith("gen-") for identifier in generations
        ), "router generation id not captured"
        deadline = asyncio.get_running_loop().time() + 60.0
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + settings.openrouter_api_key}, timeout=5
        ) as client:
            providers = [
                await _provider(client, identifier, deadline) for identifier in generations
            ]
        (tmp_path / "providers.json").write_text(
            json.dumps(
                [
                    {"generation_id": identifier, "provider_name": provider}
                    for identifier, provider in zip(generations, providers, strict=True)
                ]
            ),
            encoding="utf-8",
        )
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
