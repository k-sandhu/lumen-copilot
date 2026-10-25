"""ADR-0016/#763 cache partition isolation and transport fail-safes."""

from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import litellm
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.domain.llm import ChatMessage, Role, ToolSpec
from app.llm.gateway import LLMGateway

_CONVO = [
    ChatMessage(role=Role.SYSTEM, content="sys prompt"),
    ChatMessage(role=Role.USER, content="first question"),
    ChatMessage(role=Role.ASSISTANT, content="first answer"),
    ChatMessage(role=Role.USER, content="follow-up"),
]
_TOOLS = (
    ToolSpec(
        name="lookup", description="synthetic", parameters={"type": "object", "properties": {}}
    ),
)


def _settings(**overrides: str) -> Settings:
    values: dict[str, Any] = {
        "OPENROUTER_API_KEY": "synthetic-unit-key",
        "LLM_MODEL": "openrouter/openai/gpt-6-luna",
        **overrides,
    }
    return Settings(**values)


def _capture_acompletion(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class Stream:
        async def __aiter__(self) -> AsyncIterator[SimpleNamespace]:
            yield SimpleNamespace(
                usage=None,
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="ok", tool_calls=[]), finish_reason="stop"
                    )
                ],
            )

    async def completion(**kwargs: Any) -> Stream:
        captured.clear()
        captured.update(kwargs)
        return Stream()

    monkeypatch.setattr(litellm, "acompletion", completion)
    return captured


def _has_cache_control(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(part, dict) and "cache_control" in part for part in content
    )


_IDS = {"tenant_id": UUID(int=11), "user_id": UUID(int=22), "session_id": UUID(int=33)}
_SECRET = base64.b64encode(bytes(range(32))).decode()
_DEV = "bHVtZW4tbG9jYWwtZGV2LWNhY2hlLXNjb3BlLTAwMDA="


def _expected(ids: dict[str, UUID], secret: str = _SECRET) -> dict[str, str]:
    def digest(label: str, identity: UUID) -> str:
        return hmac.new(
            base64.b64decode(secret, validate=True),
            f"v1:{label}:{ids['tenant_id']}:{identity}".encode(),
            hashlib.sha256,
        ).hexdigest()[:24]

    return {
        "prompt_cache_key": "lc1u-" + digest("user", ids["user_id"]),
        "session_id": "lc1s-" + digest("session", ids["session_id"]),
    }


@pytest.mark.parametrize(
    ("model", "base", "partition", "breakpoints"),
    [
        ("openrouter/openai/gpt-6-luna", None, True, False),
        ("openrouter/anthropic/claude-haiku-4.5", None, False, True),
        ("openrouter/deepseek/deepseek-v4.1-flash", None, False, False),
        ("openrouter/z-ai/glm-5.3-flash", None, False, False),
        ("openai/gpt-6-luna", "https://openrouter.ai/api/v1", True, False),
        ("anthropic/claude-haiku-4.5", "https://openrouter.ai/api/v1", False, True),
    ],
)
async def test_route_payload(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    base: str | None,
    partition: bool,
    breakpoints: bool,
) -> None:
    captured = _capture_acompletion(monkeypatch)
    gateway = LLMGateway(_settings(CHAT_PROMPT_CACHE_SCOPE_SECRET=_SECRET))
    _ = [
        e
        async for e in gateway.stream_tools(
            _CONVO,
            tools=_TOOLS,
            model=model,
            api_base=base,
            tenant_id=_IDS["tenant_id"],
            user_id=_IDS["user_id"],
            session_id=_IDS["session_id"],
        )
    ]
    expected = _expected(_IDS)
    if not partition:
        expected.pop("prompt_cache_key")
    assert captured["extra_body"] == expected
    assert [_has_cache_control(m) for m in captured["messages"]] == (
        [True, False, False, True] if breakpoints else [False] * 4
    )
    fields = json.dumps(captured["extra_body"])
    assert all(str(identity) not in fields for identity in _IDS.values())
    assert all(m.content not in fields for m in _CONVO)
    assert all(len(value) == 29 for value in expected.values())


@pytest.mark.parametrize(
    ("model", "base", "enabled"),
    [
        ("openrouter/openai/gpt-6-luna", "https://other.example/v1", "true"),
        ("openrouter/anthropic/claude-haiku-4.5", "https://openrouter.ai.evil.test/v1", "true"),
        ("openai/gpt-6-luna", None, "true"),
        ("openrouter/openai/gpt-6-luna", None, "false"),
        ("openrouter/anthropic/claude-haiku-4.5", None, "false"),
    ],
)
async def test_unapproved_transport_and_kill_switch(
    monkeypatch: pytest.MonkeyPatch, model: str, base: str | None, enabled: str
) -> None:
    captured = _capture_acompletion(monkeypatch)
    gateway = LLMGateway(
        _settings(CHAT_PROMPT_CACHE_SCOPE_SECRET=_SECRET, CHAT_PROMPT_CACHE_ENABLED=enabled)
    )
    _ = [
        e
        async for e in gateway.stream_tools(
            _CONVO,
            tools=_TOOLS,
            model=model,
            api_base=base,
            tenant_id=_IDS["tenant_id"],
            user_id=_IDS["user_id"],
            session_id=_IDS["session_id"],
        )
    ]
    assert "extra_body" not in captured
    assert not any(_has_cache_control(m) for m in captured["messages"])


@pytest.mark.parametrize("missing", list(_IDS))
@pytest.mark.parametrize(
    "model", ["openrouter/openai/gpt-6-luna", "openrouter/anthropic/claude-haiku-4.5"]
)
async def test_missing_id_never_mints_shared_scope(
    monkeypatch: pytest.MonkeyPatch, missing: str, model: str
) -> None:
    captured = _capture_acompletion(monkeypatch)
    gateway = LLMGateway(_settings(CHAT_PROMPT_CACHE_SCOPE_SECRET=_SECRET))
    for value in (None, ""):
        ids: dict[str, Any] = {**_IDS, missing: value}
        _ = [e async for e in gateway.stream_tools(_CONVO, tools=_TOOLS, model=model, **ids)]
        assert "extra_body" not in captured
        assert not any(_has_cache_control(m) for m in captured["messages"])


async def test_isolation_rotation_and_vault_independence(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_acompletion(monkeypatch)

    async def payload(ids: dict[str, UUID], **settings: str) -> dict[str, str]:
        gateway = LLMGateway(
            _settings(
                LLM_MODEL="openrouter/openai/gpt-6-luna",
                CHAT_PROMPT_CACHE_SCOPE_SECRET=settings.pop(
                    "CHAT_PROMPT_CACHE_SCOPE_SECRET", _SECRET
                ),
                **settings,
            )
        )
        _ = [
            e
            async for e in gateway.stream_tools(
                _CONVO,
                tools=_TOOLS,
                tenant_id=ids["tenant_id"],
                user_id=ids["user_id"],
                session_id=ids["session_id"],
            )
        ]
        return dict(captured["extra_body"])

    original = await payload(_IDS)
    assert original == _expected(_IDS) == await payload(_IDS)
    other_session = await payload({**_IDS, "session_id": UUID(int=44)})
    assert other_session["prompt_cache_key"] == original["prompt_cache_key"]
    assert other_session["session_id"] != original["session_id"]
    assert (await payload({**_IDS, "user_id": UUID(int=55)}))["prompt_cache_key"] != original[
        "prompt_cache_key"
    ]
    other_tenant = await payload({**_IDS, "tenant_id": UUID(int=66)})
    assert all(other_tenant[key] != original[key] for key in original)
    assert (
        await payload(_IDS, SECRETS_ENCRYPTION_KEY=base64.b64encode(b"v" * 32).decode()) == original
    )
    rotated = await payload(
        _IDS, CHAT_PROMPT_CACHE_SCOPE_SECRET=base64.b64encode(b"s" * 32).decode()
    )
    assert all(rotated[key] != original[key] for key in original)
    for purpose, identifier, key in (
        ("user", "user_id", "prompt_cache_key"),
        ("session", "session_id", "session_id"),
    ):
        unkeyed = hashlib.sha256(
            f"v1:{purpose}:{_IDS['tenant_id']}:{_IDS[identifier]}".encode()
        ).hexdigest()[:24]
        assert not original[key].endswith(unkeyed)


@pytest.mark.parametrize(
    "secret", ["not-base64", "_" * 44, base64.b64encode(b"x" * 31).decode(), _SECRET.rstrip("=")]
)
def test_invalid_secret(secret: str) -> None:
    with pytest.raises(ValidationError, match="CHAT_PROMPT_CACHE_SCOPE_SECRET"):
        _settings(CHAT_PROMPT_CACHE_SCOPE_SECRET=secret)


@pytest.mark.parametrize("environment", ["test", "production", "staging"])
def test_public_default_outside_local(environment: str) -> None:
    with pytest.raises(ValidationError, match="CHAT_PROMPT_CACHE_SCOPE_SECRET"):
        _settings(ENVIRONMENT=environment, CHAT_PROMPT_CACHE_SCOPE_SECRET=_DEV)


def test_local_default_and_longer_valid_key() -> None:
    assert (
        _settings(
            ENVIRONMENT="local", CHAT_PROMPT_CACHE_SCOPE_SECRET=_DEV
        ).chat_prompt_cache_scope_secret
        == _DEV
    )
    longer = base64.b64encode(b"r" * 64).decode()
    assert _settings(CHAT_PROMPT_CACHE_SCOPE_SECRET=longer).chat_prompt_cache_scope_secret == longer


def test_no_vault_imports() -> None:
    for source in (Path(__file__).parents[1] / "app" / "llm").rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("cryptography") for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("cryptography", "app.core.crypto"))


async def test_pair_stable_across_failover_and_forced_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_acompletion(monkeypatch)
    gateway = LLMGateway(_settings(CHAT_PROMPT_CACHE_SCOPE_SECRET=_SECRET))
    for model, choice in (
        ("openrouter/openai/primary", None),
        ("openrouter/openai/fallback", "none"),
    ):
        _ = [
            e
            async for e in gateway.stream_tools(
                _CONVO,
                tools=_TOOLS,
                model=model,
                tool_choice=choice,
                tenant_id=_IDS["tenant_id"],
                user_id=_IDS["user_id"],
                session_id=_IDS["session_id"],
            )
        ]
        assert captured["extra_body"] == _expected(_IDS)


@pytest.mark.parametrize("environment", ["local", "production"])
def test_noncanonical_spelling_cannot_bypass_public_key_guard(environment: str) -> None:
    alternate = "bHVtZW4tbG9jYWwtZGV2LWNhY2hlLXNjb3BlLTAwMDB="
    assert base64.b64decode(alternate, validate=True) == base64.b64decode(_DEV, validate=True)
    with pytest.raises(ValidationError, match="CHAT_PROMPT_CACHE_SCOPE_SECRET"):
        _settings(ENVIRONMENT=environment, CHAT_PROMPT_CACHE_SCOPE_SECRET=alternate)
