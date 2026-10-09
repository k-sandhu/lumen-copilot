"""Dedicated durable-audit pool configuration guardrails (#579, R1-001)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

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


def test_durable_audit_pool_defaults_are_small_and_bounded() -> None:
    settings = Settings(_env_file=None, **_BASE)

    assert settings.audit_db_pool_size == 4
    assert settings.audit_db_pool_timeout_seconds == 2
    assert settings.audit_db_operation_timeout_seconds == 5


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("AUDIT_DB_POOL_SIZE", 0),
        ("AUDIT_DB_POOL_SIZE", 65),
        ("AUDIT_DB_POOL_TIMEOUT_SECONDS", 0),
        ("AUDIT_DB_OPERATION_TIMEOUT_SECONDS", 0),
    ],
)
def test_invalid_durable_audit_pool_bounds_fail_fast(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_BASE, **{field: value})
