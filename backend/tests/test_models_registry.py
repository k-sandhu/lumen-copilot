"""Chat-model registry unit tests — config + service helper (#47).

Covers, without any HTTP or DB:
* AC-2: the registry is config-driven — the seed set lives in settings, and a
  CHAT_MODEL_REGISTRY JSON override replaces it; bad overrides fail at startup;
* AC-1: the settings validator enforces non-empty / unique-id / exactly-one
  default;
* AC-3: ``is_allowed_model`` accepts a registered id and rejects an unknown one
  (the basis for the future chat-runtime 422 / INV-8).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.core.config import _DEFAULT_CHAT_MODEL_REGISTRY, Settings
from app.domain.models import ChatModel, ModelTier
from app.services.models_service import ChatModelService, is_allowed_model

# Minimal valid infra env so Settings constructs in isolation (mirrors conftest).
_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://t:t@localhost:5432/t",
    "REDIS_URL": "redis://localhost:6379/0",
    "CELERY_BROKER_URL": "redis://localhost:6379/1",
    "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_ACCESS_KEY": "t",
    "S3_SECRET_KEY": "t",
    "S3_BUCKET": "b",
    "ENVIRONMENT": "local",
}


def _settings(**overrides: str) -> Settings:
    """Build a Settings from explicit values (ignores the process env/.env)."""
    return Settings(_env_file=None, **{**_BASE_ENV, **overrides})  # type: ignore[call-arg]


# --- AC-2: config-driven seed registry --------------------------------------


def test_default_registry_is_the_seed_set() -> None:
    settings = _settings()
    ids_by_tier: dict[ModelTier, set[str]] = {t: set() for t in ModelTier}
    for m in settings.chat_model_registry:
        ids_by_tier[m.tier].add(m.id)

    assert ids_by_tier[ModelTier.FRONTIER] == {
        "openrouter/anthropic/claude-opus-4.8",
        "openrouter/openai/gpt-5.5",
    }
    assert ids_by_tier[ModelTier.FAST] == {
        "openrouter/google/gemini-3.5-flash",
        "openrouter/anthropic/claude-haiku-4.5",
    }
    assert ids_by_tier[ModelTier.OSS] == {
        "openrouter/deepseek/deepseek-v3.2",
        "openrouter/qwen/qwen3.7-max",
    }


def test_seed_registry_has_exactly_one_default() -> None:
    defaults = [m for m in _DEFAULT_CHAT_MODEL_REGISTRY if m.is_default]
    # #490 AC-1: the shipped default moved off the frontier tier (it set the
    # floor for perceived chat speed — #486) onto a FAST-tier entry.
    assert [m.id for m in defaults] == ["openrouter/anthropic/claude-haiku-4.5"]


def test_seed_default_is_fast_tier_not_frontier() -> None:
    """#490 AC-1: exactly one ``is_default`` and its tier is FAST, never FRONTIER.

    A frontier default is inherited by two side calls (summaries, suggestions)
    and buffered whole before the user sees it, so it sets the floor for
    perceived chat speed (#486). This is the invariant that keeps the flip in
    place; reverting it is a single registry line, caught here.
    """
    defaults = [m for m in _DEFAULT_CHAT_MODEL_REGISTRY if m.is_default]
    assert len(defaults) == 1
    assert defaults[0].tier is ModelTier.FAST
    assert defaults[0].tier is not ModelTier.FRONTIER


def test_frontier_entries_remain_present_and_selectable() -> None:
    """#490 AC-3: frontier stays in the registry (an explicit per-session /
    per-tenant choice), it is merely no longer the default."""
    settings = _settings()
    frontier_ids = {m.id for m in settings.chat_model_registry if m.tier is ModelTier.FRONTIER}
    assert frontier_ids == {
        "openrouter/anthropic/claude-opus-4.8",
        "openrouter/openai/gpt-5.5",
    }
    # Still allow-listed, so a session or tenant may pin it (the picker + the
    # per-tenant provider overrides drive that selection).
    for fid in frontier_ids:
        assert is_allowed_model(fid, settings) is True


def test_llm_model_is_not_the_chat_default() -> None:
    """#490 AC-5: ``LLM_MODEL`` is the gateway fallback for callers that pass
    ``model=None`` (in practice only the /search direct answer), NOT the chat
    default. Chat resolves its default from the registry ``is_default`` entry
    (``ChatService._default_model``). They are deliberately different values;
    this guards against silently conflating the two (the config trap #490
    documents in .env.example and the ``llm_model`` field description)."""
    settings = _settings()
    chat_default = next(m.id for m in settings.chat_model_registry if m.is_default)
    assert settings.llm_model != chat_default


def test_registry_override_via_env_json_replaces_seed() -> None:
    override = json.dumps(
        [
            {
                "id": "openrouter/anthropic/claude-opus-4.8",
                "label": "Opus",
                "provider": "anthropic",
                "tier": "frontier",
                "is_default": True,
            },
            {
                "id": "openrouter/openai/gpt-5.5",
                "label": "GPT",
                "provider": "openai",
                "tier": "frontier",
            },
        ]
    )
    settings = _settings(CHAT_MODEL_REGISTRY=override)
    assert [m.id for m in settings.chat_model_registry] == [
        "openrouter/anthropic/claude-opus-4.8",
        "openrouter/openai/gpt-5.5",
    ]


def test_blank_registry_env_falls_back_to_seed() -> None:
    settings = _settings(CHAT_MODEL_REGISTRY="")
    assert settings.chat_model_registry == _DEFAULT_CHAT_MODEL_REGISTRY


# --- AC-1: registry invariants fail closed at startup -----------------------


def test_registry_without_default_is_rejected() -> None:
    bad = json.dumps([{"id": "a/b", "label": "B", "provider": "a", "tier": "fast"}])
    with pytest.raises(ValidationError):
        _settings(CHAT_MODEL_REGISTRY=bad)


def test_registry_with_two_defaults_is_rejected() -> None:
    bad = json.dumps(
        [
            {"id": "a/b", "label": "B", "provider": "a", "tier": "fast", "is_default": True},
            {"id": "c/d", "label": "D", "provider": "c", "tier": "oss", "is_default": True},
        ]
    )
    with pytest.raises(ValidationError):
        _settings(CHAT_MODEL_REGISTRY=bad)


def test_registry_with_duplicate_ids_is_rejected() -> None:
    bad = json.dumps(
        [
            {"id": "a/b", "label": "B", "provider": "a", "tier": "fast", "is_default": True},
            {"id": "a/b", "label": "B2", "provider": "a", "tier": "oss"},
        ]
    )
    with pytest.raises(ValidationError):
        _settings(CHAT_MODEL_REGISTRY=bad)


def test_empty_registry_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(CHAT_MODEL_REGISTRY="[]")


def test_unknown_tier_is_rejected() -> None:
    bad = json.dumps(
        [{"id": "a/b", "label": "B", "provider": "a", "tier": "nope", "is_default": True}]
    )
    with pytest.raises(ValidationError):
        _settings(CHAT_MODEL_REGISTRY=bad)


# --- AC-3: is_allowed_model (basis for the future chat-runtime 422) ---------


def test_is_allowed_model_accepts_registered_id() -> None:
    settings = _settings()
    assert is_allowed_model("openrouter/anthropic/claude-opus-4.8", settings) is True
    assert is_allowed_model("openrouter/qwen/qwen3.7-max", settings) is True


def test_is_allowed_model_rejects_unknown_id() -> None:
    settings = _settings()
    # An id not in the registry → not allowed (the chat runtime turns this into a 422).
    assert is_allowed_model("acme/not-a-real-model", settings) is False
    # Empty / whitespace are likewise not allowed.
    assert is_allowed_model("", settings) is False


def test_is_allowed_model_is_exact_match_not_prefix() -> None:
    settings = _settings()
    # A near-miss (prefix / suffix of a real id) is rejected — exact match only.
    assert is_allowed_model("anthropic/claude-opus", settings) is False
    assert is_allowed_model("anthropic/claude-opus-4.8-turbo", settings) is False


# --- Service surface --------------------------------------------------------


def test_service_lists_registry_as_domain_models() -> None:
    settings = _settings()
    service = ChatModelService(settings)
    models = service.list_models()

    assert all(isinstance(m, ChatModel) for m in models)
    assert {m.id for m in models} == {m.id for m in settings.chat_model_registry}
    # Order is preserved from config (frontier → fast → oss in the seed).
    assert [m.id for m in models] == [m.id for m in settings.chat_model_registry]


def test_service_is_allowed_model_matches_free_function() -> None:
    settings = _settings()
    service = ChatModelService(settings)
    assert service.is_allowed_model("openrouter/openai/gpt-5.5") is True
    assert service.is_allowed_model("openai/gpt-0") is False
