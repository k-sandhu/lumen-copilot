"""Shared valid Settings fixture for reusable-sandbox tests (ADR-0020)."""

from __future__ import annotations

from app.core.config import Settings


def sandbox_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "DATABASE_URL": "sqlite+aiosqlite://",
        "REDIS_URL": "redis://localhost:6379/0",
        "CELERY_BROKER_URL": "redis://localhost:6379/1",
        "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_ACCESS_KEY": "lumen",
        "S3_SECRET_KEY": "lumen_local_dev_secret",
        "S3_BUCKET": "b",
        "OPENROUTER_API_KEY": "",
        "SANDBOX_ENABLED": "true",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


__all__ = ["sandbox_settings"]
