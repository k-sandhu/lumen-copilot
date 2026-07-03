"""Sandbox config tests — default-OFF + fail-fast validators (ADR-0013 §2/§6, #230).

The sandbox is the highest-risk capability, so its config must fail closed: code
execution is **disabled by default** (the kill-switch), and every load-bearing
resource cap / quota must be positive (a non-positive value would disable an
isolation control) — a misconfiguration refuses to boot rather than launching runs
unbounded.
"""

from __future__ import annotations

import pytest

from tests._sandbox_helpers import sandbox_settings


def test_sandbox_disabled_by_default() -> None:
    """The kill-switch: code execution is OFF for every tenant until explicitly enabled."""
    settings = sandbox_settings(SANDBOX_ENABLED="false")
    assert settings.sandbox_enabled is False


def test_sandbox_default_omitted_is_off() -> None:
    """With no override at all, ``sandbox_enabled`` defaults to False (ADR-0013 §6)."""
    from app.core.config import Settings

    settings = Settings(  # type: ignore[call-arg]
        DATABASE_URL="sqlite+aiosqlite://",
        REDIS_URL="redis://localhost:6379/0",
        CELERY_BROKER_URL="redis://localhost:6379/1",
        CELERY_RESULT_BACKEND="redis://localhost:6379/2",
        S3_ENDPOINT_URL="http://localhost:9000",
        S3_ACCESS_KEY="k",
        S3_SECRET_KEY="s",
        S3_BUCKET="b",
        OPENROUTER_API_KEY="",
    )
    assert settings.sandbox_enabled is False


@pytest.mark.parametrize(
    "field",
    [
        "SANDBOX_CPUS",
        "SANDBOX_MEMORY_BYTES",
        "SANDBOX_PIDS_LIMIT",
        "SANDBOX_WALL_CLOCK_SECONDS",
        "SANDBOX_OUTPUT_BYTES_CAP",
        "SANDBOX_SCRATCH_BYTES",
        "SANDBOX_MAX_CONCURRENT_PER_TENANT",
        "SANDBOX_DAILY_RUNTIME_SECONDS_PER_TENANT",
    ],
)
def test_non_positive_limit_or_quota_rejected(field: str) -> None:
    """A zero/negative resource cap or quota would disable an isolation control — reject."""
    with pytest.raises(ValueError, match="must be positive"):
        sandbox_settings(**{field: "0"})


def test_unknown_runtime_rejected() -> None:
    """Only ``runc`` (Docker baseline) or ``runsc`` (gVisor) are valid runtimes."""
    with pytest.raises(ValueError, match="runc.*runsc|runsc"):
        sandbox_settings(SANDBOX_RUNTIME="firecracker")


@pytest.mark.parametrize("runtime", ["runc", "runsc"])
def test_known_runtimes_accepted(runtime: str) -> None:
    """gVisor is a config swap — both runtimes construct cleanly."""
    settings = sandbox_settings(SANDBOX_RUNTIME=runtime)
    assert settings.sandbox_runtime == runtime
