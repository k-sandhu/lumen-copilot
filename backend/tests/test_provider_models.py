"""Unit tests for the provider-model id namespacing helpers (PR 2a).

Pure functions — no DB. Locks the ``provider:{provider_id}:{raw_model_id}`` id
shape the surfacing layer builds and the allow-list / chat runtime parse: a config
id is never mistaken for a provider id, a raw id that itself contains colons
survives a round-trip, and a malformed id parses to ``None`` (⇒ unknown model 422)
rather than crashing.
"""

from __future__ import annotations

import uuid

from app.services.provider_models import (
    is_provider_model_id,
    make_provider_model_id,
    parse_provider_model_id,
)


def test_make_and_parse_round_trip() -> None:
    pid = uuid.uuid4()
    model_id = make_provider_model_id(pid, "openai/gpt-4o")
    assert model_id == f"provider:{pid}:openai/gpt-4o"
    assert is_provider_model_id(model_id)
    assert parse_provider_model_id(model_id) == (pid, "openai/gpt-4o")


def test_raw_id_with_colons_survives() -> None:
    # Only the first two colons are structural, so a raw id containing colons is
    # preserved intact (split on the first two only).
    pid = uuid.uuid4()
    raw = "vendor:family:model-v2"
    parsed = parse_provider_model_id(make_provider_model_id(pid, raw))
    assert parsed == (pid, raw)


def test_config_id_is_not_a_provider_id() -> None:
    assert not is_provider_model_id("openrouter/anthropic/claude-opus-4.8")
    assert parse_provider_model_id("openrouter/anthropic/claude-opus-4.8") is None


def test_malformed_provider_ids_parse_to_none() -> None:
    # Bad UUID, missing raw id, and empty raw id are all unknown, not crashes.
    assert parse_provider_model_id("provider:not-a-uuid:openai/gpt-4o") is None
    assert parse_provider_model_id(f"provider:{uuid.uuid4()}") is None
    assert parse_provider_model_id(f"provider:{uuid.uuid4()}:") is None
