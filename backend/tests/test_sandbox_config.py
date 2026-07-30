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


# --- The image the tenant code actually runs in is pinned at LAUNCH time ------
#
# ADR-0013 §3 asks for an execution image "pinned by digest, no ``:latest``". The
# ``sandbox_exec/Dockerfile`` FROM line pins its BASE by digest, but that is a
# BUILD-time fact about a layer; the reference the runner launches comes from
# ``SANDBOX_IMAGE`` at run time. Until these validators existed that value took
# ``:latest``, a tagless name, or a remote ref unchallenged — so the documented
# guarantee applied to the base layer while the thing model code executes in was a
# mutable tag.

_DIGEST = "sha256:" + "a" * 64


def test_default_execution_image_is_tag_pinned_and_not_latest() -> None:
    """The shipped default names an exact tag — never a floating one."""
    image = sandbox_settings().sandbox_image

    assert ":latest" not in image
    assert ":" in image.rsplit("/", 1)[-1]


@pytest.mark.parametrize(
    "image",
    [
        "lumen-sandbox-exec:latest",
        "lumen-sandbox-exec",
        "ghcr.io/lumen/lumen-sandbox-exec",
        "registry.internal:5000/lumen-sandbox-exec",
        "lumen-sandbox-exec@sha256:abc",
    ],
)
def test_floating_or_malformed_execution_image_is_rejected(image: str) -> None:
    """A mutable or unparseable reference refuses to boot rather than run code."""
    with pytest.raises(ValueError, match="SANDBOX_IMAGE"):
        sandbox_settings(SANDBOX_IMAGE=image)


@pytest.mark.parametrize(
    "image",
    [f"lumen-sandbox-exec@{_DIGEST}", f"lumen-sandbox-exec:0.1.0@{_DIGEST}"],
)
def test_digest_pinned_execution_image_is_accepted(image: str) -> None:
    """``name@sha256:…`` (with or without a readability tag) is the strongest pin."""
    assert sandbox_settings(SANDBOX_IMAGE=image).sandbox_image == image


def _production_env(**overrides: object) -> dict[str, object]:
    """The minimal non-local boot env (see test_sandbox_isolation for the pedigree)."""
    base: dict[str, object] = {
        "ENVIRONMENT": "production",
        "JWT_SECRET": "production-secret-that-is-not-the-dev-default",
        "SECRETS_ENCRYPTION_KEY": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        "CONNECTOR_OAUTH_REDIRECT_BASE_URL": "https://api.example.com",
        "CONNECTOR_OAUTH_FRONTEND_RETURN_URL": "https://app.example.com/sources",
        "GDRIVE_OAUTH_CLIENT_ID": "prod-google-client-id",
        "GDRIVE_OAUTH_CLIENT_SECRET": "prod-google-client-secret",
        "SANDBOX_RUNTIME": "runsc",
    }
    base.update(overrides)
    return base


def test_enabled_sandbox_outside_local_requires_a_digest_pinned_image() -> None:
    """A tag is trust-on-first-build; outside local dev the digest is mandatory."""
    with pytest.raises(ValueError, match="@sha256"):
        sandbox_settings(**_production_env(SANDBOX_IMAGE="lumen-sandbox-exec:0.1.0"))

    settings = sandbox_settings(**_production_env(SANDBOX_IMAGE=f"lumen-sandbox-exec@{_DIGEST}"))
    assert settings.sandbox_image.endswith(_DIGEST)


def test_disabled_sandbox_outside_local_still_boots_on_the_tag_default() -> None:
    """The digest requirement follows the capability, not the environment alone.

    A deploy with code execution OFF launches nothing, so holding its boot hostage
    to an image reference it never uses would be a gratuitous outage.
    """
    settings = sandbox_settings(**_production_env(SANDBOX_ENABLED="false"))

    assert settings.sandbox_enabled is False
    assert settings.sandbox_image == "lumen-sandbox-exec:0.1.0"
