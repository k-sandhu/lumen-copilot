"""R1-005/R1-006 compatibility-gate and side-effect-free readiness tests."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

import app.api.health as health_module
import app.ingestion.contract as contract_module
from app.core.config import Settings
from app.core.errors import DependencyError
from app.domain.llm import Embedding


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "DATABASE_URL": "sqlite+aiosqlite://",
        "REDIS_URL": "redis://localhost:6379/0",
        "CELERY_BROKER_URL": "redis://localhost:6379/1",
        "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_ACCESS_KEY": "k",
        "S3_SECRET_KEY": "s",
        "S3_BUCKET": "b",
        "OPENROUTER_API_KEY": "configured-for-test",
        "LLM_EMBEDDING_DIMENSIONS": 8,
        **overrides,
    }
    return Settings(**values)  # type: ignore[arg-type]


async def test_readiness_mapping_check_is_strictly_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A health request performs GET validation, never index/pipeline writes."""

    settings = _settings()

    class _ReadOnlyStore:
        checked = 0
        closed = 0

        async def ensure_index(self) -> None:  # pragma: no cover - forbidden path
            raise AssertionError("readiness must not provision OpenSearch")

        async def check_embedding_contract(self) -> tuple[int, str]:
            self.checked += 1
            return settings.llm_embedding_dimensions, settings.embedding_space_fingerprint

        async def aclose(self) -> None:
            self.closed += 1

    store = _ReadOnlyStore()
    monkeypatch.setattr(
        health_module.OpenSearchStore,
        "from_settings",
        lambda _settings: store,
    )

    result = await health_module._check_opensearch_embedding(settings)

    assert result.ok is True
    assert store.checked == 1
    assert store.closed == 1


async def test_readiness_reads_cached_provider_fingerprint_without_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider outages/cost cannot be amplified by polling health endpoints."""

    settings = _settings()
    contract_module.mark_embedding_contract_valid(settings.embedding_space_fingerprint)
    # There is intentionally no LLMGateway import in health.py. This sentinel
    # also pins that readiness asks only the contract-gate function.
    calls = 0

    def _status(fingerprint: str) -> tuple[bool, str | None]:
        nonlocal calls
        calls += 1
        assert fingerprint == settings.embedding_space_fingerprint
        return True, None

    monkeypatch.setattr(health_module, "embedding_contract_status", _status)

    first = await health_module._check_embedding_provider(settings)
    second = await health_module._check_embedding_provider(settings)

    assert first.ok and second.ok
    assert calls == 2


async def test_worker_preflight_caches_only_a_fully_validated_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    counts = {"schema": 0, "ensure": 0, "check": 0, "provider": 0}

    async def _schema(_settings: Settings) -> object:
        counts["schema"] += 1
        return object()

    class _Store:
        async def ensure_index(self) -> None:
            counts["ensure"] += 1

        async def check_embedding_contract(self) -> tuple[int, str]:
            counts["check"] += 1
            return settings.llm_embedding_dimensions, settings.embedding_space_fingerprint

        async def aclose(self) -> None:
            return None

    class _Gateway:
        def __init__(self, _settings: Settings) -> None:
            pass

        async def embed(
            self,
            inputs: Sequence[str],
            *,
            cache_namespace: str | None = None,
        ) -> list[Embedding]:
            counts["provider"] += 1
            return [Embedding(vector=[0.0] * 8, model="test") for _ in inputs]

    contract_module.reset_embedding_contract_gate()
    monkeypatch.setattr(contract_module, "check_embedding_schema", _schema)
    monkeypatch.setattr(
        contract_module.OpenSearchStore,
        "from_settings",
        lambda _settings: _Store(),
    )
    monkeypatch.setattr(contract_module, "LLMGateway", _Gateway)

    first = await contract_module.ensure_embedding_contract(settings)
    second = await contract_module.ensure_embedding_contract(settings)

    assert first == second == settings.embedding_space_fingerprint
    assert counts == {"schema": 1, "ensure": 1, "check": 1, "provider": 1}


async def test_failed_provider_preflight_never_caches_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()

    async def _schema(_settings: Settings) -> object:
        return object()

    class _Store:
        async def ensure_index(self) -> None:
            return None

        async def check_embedding_contract(self) -> tuple[int, str]:
            return settings.llm_embedding_dimensions, settings.embedding_space_fingerprint

        async def aclose(self) -> None:
            return None

    class _Gateway:
        def __init__(self, _settings: Settings) -> None:
            pass

        async def embed(self, *_args: object, **_kwargs: object) -> list[Embedding]:
            raise DependencyError("provider unavailable", code="embedding_provider_unavailable")

    contract_module.reset_embedding_contract_gate()
    monkeypatch.setattr(contract_module, "check_embedding_schema", _schema)
    monkeypatch.setattr(
        contract_module.OpenSearchStore,
        "from_settings",
        lambda _settings: _Store(),
    )
    monkeypatch.setattr(contract_module, "LLMGateway", _Gateway)

    with pytest.raises(DependencyError):
        await contract_module.provision_embedding_contract(settings)
    assert contract_module.embedding_contract_status(settings.embedding_space_fingerprint) == (
        False,
        "embedding_provider_unavailable",
    )


async def test_untyped_preflight_outage_is_normalized_and_never_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1-003/R1-005: DB/startup faults enter typed worker retry semantics."""

    settings = _settings()

    async def _database_down(_settings: Settings) -> object:
        raise RuntimeError("credential-bearing driver detail")

    contract_module.reset_embedding_contract_gate()
    monkeypatch.setattr(contract_module, "check_embedding_schema", _database_down)

    with pytest.raises(DependencyError) as excinfo:
        await contract_module.provision_embedding_contract(settings)

    assert excinfo.value.code == "embedding_contract_preflight_failed"
    assert "credential-bearing" not in str(excinfo.value)
    assert contract_module.embedding_contract_status(settings.embedding_space_fingerprint) == (
        False,
        "embedding_contract_preflight_failed",
    )


async def test_unconfigured_provider_fails_closed_before_schema_or_index_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1-005: no key is degraded/deferred, never a falsely validated contract."""

    settings = _settings(OPENROUTER_API_KEY="")

    async def _forbidden_schema(_settings: Settings) -> object:
        raise AssertionError("provider configuration should fail before provisioning")

    contract_module.reset_embedding_contract_gate()
    monkeypatch.setattr(contract_module, "check_embedding_schema", _forbidden_schema)

    with pytest.raises(DependencyError) as excinfo:
        await contract_module.provision_embedding_contract(settings)

    assert excinfo.value.code == "llm_unconfigured"
    assert contract_module.ingestion_enqueue_allowed(settings.embedding_space_fingerprint) is False
