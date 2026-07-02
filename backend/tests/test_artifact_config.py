"""Artifact-store config guardrails (issue #208 §5).

Pin the three artifact settings: the size cap + content-type allowlist defaults
and their env overrides, and the retention-days validator (positive when set,
``None`` = keep forever). The allowlist must be **broader** than the upload one
(agent output is more varied) and the cap must fail fast on a nonsense retention
window rather than silently deleting artifacts.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

# Minimal valid base env so Settings constructs; individual tests override one
# field to exercise a single artifact guardrail.
_BASE = {
    "DATABASE_URL": "postgresql+asyncpg://t:t@localhost/t",
    "REDIS_URL": "redis://localhost",
    "CELERY_BROKER_URL": "redis://localhost",
    "CELERY_RESULT_BACKEND": "redis://localhost",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_ACCESS_KEY": "t",
    "S3_SECRET_KEY": "tt",
    "S3_BUCKET": "b",
}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **_BASE, **overrides)  # type: ignore[arg-type]


def test_artifact_defaults() -> None:
    s = _settings()
    # 50 MiB cap by default (#208 §3).
    assert s.max_artifact_bytes == 50 * 1024 * 1024
    # Keep forever by default (retention is opt-in).
    assert s.artifact_retention_days is None
    # Broader than uploads: includes the agent-output formats the issue lists.
    for ct in (
        "text/csv",
        "application/json",
        "image/png",
        "image/svg+xml",
        "text/html",
    ):
        assert ct in s.artifact_allowed_content_types


def test_artifact_allowlist_is_broader_than_uploads() -> None:
    s = _settings()
    # Every upload type is not necessarily an artifact type, but the artifact set
    # is strictly larger and covers formats uploads don't (csv/json/png/svg/html).
    assert len(s.artifact_allowed_content_types) > len(s.upload_allowed_content_types)
    assert "text/csv" in s.artifact_allowed_content_types
    assert "text/csv" not in s.upload_allowed_content_types


def test_artifact_allowlist_env_override_is_comma_split() -> None:
    s = _settings(ARTIFACT_ALLOWED_CONTENT_TYPES="text/csv, application/json ,image/png")
    assert s.artifact_allowed_content_types == frozenset(
        {"text/csv", "application/json", "image/png"}
    )


def test_artifact_cap_env_override() -> None:
    s = _settings(MAX_ARTIFACT_BYTES=1234)
    assert s.max_artifact_bytes == 1234


def test_artifact_retention_days_accepts_positive() -> None:
    s = _settings(ARTIFACT_RETENTION_DAYS=30)
    assert s.artifact_retention_days == 30


@pytest.mark.parametrize("bad", [0, -1, -30])
def test_artifact_retention_days_rejects_non_positive(bad: int) -> None:
    # A zero/negative window would purge everything or be meaningless — fail fast.
    with pytest.raises(ValidationError):
        _settings(ARTIFACT_RETENTION_DAYS=bad)
