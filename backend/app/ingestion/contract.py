"""Embedding-space compatibility gate for startup, workers, and operators.

Readiness is deliberately observational: it may inspect Postgres/OpenSearch and
read this process-local gate, but it never creates schemas or calls a paid
provider. Mutating/provider validation lives here and runs at API startup, once
per worker process on first ingestion, or explicitly from the re-embed command.
Only a fully validated fingerprint is cached; a model/base/dimension change
therefore invalidates the cache and must pass the complete preflight again.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.errors import DependencyError
from app.db.embedding_contract import check_embedding_schema
from app.llm import LLMGateway
from app.search import OpenSearchStore

_validated_fingerprint: str | None = None
_last_failure_code: str | None = None


def reset_embedding_contract_gate() -> None:
    """Clear process-local compatibility state (tests/process reconfiguration)."""

    global _validated_fingerprint, _last_failure_code
    _validated_fingerprint = None
    _last_failure_code = None


def mark_embedding_contract_valid(fingerprint: str) -> None:
    """Publish exactly one successfully validated coordinate-space identity."""

    global _validated_fingerprint, _last_failure_code
    _validated_fingerprint = fingerprint
    _last_failure_code = None


def mark_embedding_contract_invalid(code: str) -> None:
    """Fail closed without retaining provider/schema details or credentials."""

    global _validated_fingerprint, _last_failure_code
    _validated_fingerprint = None
    _last_failure_code = code


def embedding_contract_status(expected_fingerprint: str) -> tuple[bool, str | None]:
    """Return cached compatibility for readiness without causing side effects."""

    if _validated_fingerprint == expected_fingerprint:
        return True, None
    if _validated_fingerprint is not None:
        return False, "embedding_space_changed"
    return False, _last_failure_code or "embedding_contract_unvalidated"


def ingestion_enqueue_allowed(expected_fingerprint: str) -> bool:
    """Defer API publication only after startup explicitly found incompatibility.

    ``None`` means this process does not own startup (for example a beat-only
    process) and remains backward-compatible; the consuming worker still runs
    :func:`ensure_embedding_contract` before claiming any document.
    """

    if _validated_fingerprint == expected_fingerprint:
        return True
    return _validated_fingerprint is None and _last_failure_code is None


async def provision_embedding_contract(settings: Settings) -> str:
    """Mutating startup/operator preflight; cache only after every gate passes."""

    try:
        if not settings.llm_enabled:
            raise DependencyError(
                "Embedding provider credentials are not configured.",
                code="llm_unconfigured",
            )
        await check_embedding_schema(settings)
        store = OpenSearchStore.from_settings(settings)
        try:
            await store.ensure_index()
            await store.check_embedding_contract()
        finally:
            await store.aclose()

        await LLMGateway(settings).embed(
            ["lumen embedding contract preflight"],
            cache_namespace="embedding-contract-preflight",
        )
    except DependencyError as exc:
        mark_embedding_contract_invalid(exc.code)
        raise
    except Exception as exc:
        mark_embedding_contract_invalid("embedding_contract_preflight_failed")
        raise DependencyError(
            "Embedding contract preflight could not reach a required dependency.",
            code="embedding_contract_preflight_failed",
        ) from exc

    mark_embedding_contract_valid(settings.embedding_space_fingerprint)
    return settings.embedding_space_fingerprint


async def ensure_embedding_contract(settings: Settings) -> str:
    """One-time worker preflight, repeated only after config/contract drift."""

    if _validated_fingerprint == settings.embedding_space_fingerprint:
        return _validated_fingerprint
    return await provision_embedding_contract(settings)


__all__ = [
    "embedding_contract_status",
    "ensure_embedding_contract",
    "ingestion_enqueue_allowed",
    "mark_embedding_contract_invalid",
    "mark_embedding_contract_valid",
    "provision_embedding_contract",
    "reset_embedding_contract_gate",
]
