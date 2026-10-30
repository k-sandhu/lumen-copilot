"""Unit tests for the provider-model id namespacing helpers (PR 2a).

Pure functions — no DB. Locks the ``provider:{provider_id}:{raw_model_id}`` id
shape the surfacing layer builds and the allow-list / chat runtime parse: a config
id is never mistaken for a provider id, a raw id that itself contains colons
survives a round-trip, and a malformed id parses to ``None`` (⇒ unknown model 422)
rather than crashing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.core.errors import ValidationError
from app.domain.entities import LlmProvider, LlmProviderStatus
from app.services import provider_models
from app.services.provider_models import (
    is_provider_model_id,
    is_routable,
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


def _provider(
    *, status: LlmProviderStatus, enabled: bool = True, grandfathered: bool = False
) -> LlmProvider:
    now = datetime.now(UTC)
    return LlmProvider(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        name="P",
        provider_type="openai_compatible",
        base_url="https://provider.example.com/v1",
        api_key_secret_ref=None,
        secret_hint=None,
        enabled=enabled,
        status=status,
        last_discovery_at=None,
        last_error=None,
        discovered_models=[{"id": "m1", "label": "M1"}],
        created_at=now,
        updated_at=now,
        validation_grandfathered=grandfathered,
    )


@pytest.mark.parametrize(
    ("status", "enabled", "grandfathered", "expected"),
    [
        (LlmProviderStatus.READY, True, False, True),
        # Grandfathered pre-existing ready rows stay routable until revalidation.
        (LlmProviderStatus.READY, True, True, True),
        (LlmProviderStatus.READY, False, False, False),
        (LlmProviderStatus.REACHABLE, True, False, False),
        (LlmProviderStatus.PENDING, True, False, False),
        (LlmProviderStatus.ERROR, True, False, False),
    ],
)
def test_only_ready_enabled_providers_are_routable(
    status: LlmProviderStatus, enabled: bool, grandfathered: bool, expected: bool
) -> None:
    assert (
        is_routable(_provider(status=status, enabled=enabled, grandfathered=grandfathered))
        is expected
    )


def test_malformed_provider_ids_parse_to_none() -> None:
    # Bad UUID, missing raw id, and empty raw id are all unknown, not crashes.
    assert parse_provider_model_id("provider:not-a-uuid:openai/gpt-4o") is None
    assert parse_provider_model_id(f"provider:{uuid.uuid4()}") is None
    assert parse_provider_model_id(f"provider:{uuid.uuid4()}:") is None


@pytest.mark.parametrize("status", [LlmProviderStatus.REACHABLE, LlmProviderStatus.ERROR])
async def test_picker_and_resolver_refuse_unverified_snapshots(
    monkeypatch: pytest.MonkeyPatch,
    status: LlmProviderStatus,
) -> None:
    from types import SimpleNamespace

    provider = _provider(status=status)
    repository = SimpleNamespace(
        get=AsyncMock(return_value=provider),
        list_for_tenant=AsyncMock(return_value=[provider]),
    )
    monkeypatch.setattr(provider_models, "LlmProviderRepository", lambda *args: repository)
    assert await provider_models.list_provider_models(repository) == []
    resolver = provider_models.build_model_route_resolver(
        settings=None,
        tenant_id=provider.tenant_id,
        owner_id=provider.owner_id,
        roles=(),
        request_id="route",
        source_ip="unknown",
    )
    with pytest.raises(ValidationError):
        await resolver(None, make_provider_model_id(provider.id, "m1"))
