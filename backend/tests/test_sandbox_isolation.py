"""Offline enforcement-wiring tests for contained root sessions (ADR-0020)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.sandbox.runner import build_container_flags
from app.sandbox.spec import SandboxSessionSpec, StagedInput
from tests._sandbox_helpers import sandbox_settings


def _session(*, runtime: str = "runsc", output_bytes_cap: int | None = None) -> SandboxSessionSpec:
    return SandboxSessionSpec(
        sandbox_session_id=uuid4(),
        generation=1,
        image="python@sha256:abc",
        runtime=runtime,
        output_bytes_cap=output_bytes_cap,
        env=(
            ("HOME", "/root"),
            ("PYTHONUNBUFFERED", "1"),
            ("MPLBACKEND", "Agg"),
            ("LANG", "C.UTF-8"),
        ),
    )


def test_root_is_contained_without_host_mounts_socket_or_network() -> None:
    flags = build_container_flags(_session())
    assert flags.user == "0:0"
    assert flags.read_only_rootfs is False
    assert flags.binds == ()
    assert flags.network_mode == "none"
    assert flags.cap_drop == ("ALL",)
    assert flags.security_opt == ("no-new-privileges:true",)


def test_no_automatic_time_resource_or_pid_limits() -> None:
    flags = build_container_flags(_session())
    assert flags.wall_clock_seconds is None
    assert flags.cpus is None
    assert flags.memory_bytes is None
    assert flags.pids_limit is None


def test_output_collection_is_bounded_by_the_configured_cap() -> None:
    """The one limit ADR-0020 does enforce, because it protects OTHER tenants.

    Collected output is read into the runner's memory, and the runner is the single
    holder of the Docker socket: an unbounded collection let one chat turn OOM the
    process every tenant's code execution depends on. ``output_bytes_cap`` existed as
    a field that nothing populated and nothing read; it is now carried to the runner.
    """
    settings = sandbox_settings()
    flags = build_container_flags(_session(output_bytes_cap=settings.sandbox_output_bytes_cap))

    assert flags.output_bytes_cap == settings.sandbox_output_bytes_cap
    assert flags.output_bytes_cap is not None and flags.output_bytes_cap > 0


def test_curated_session_env_contains_no_application_secret_keys() -> None:
    keys = {key for key, _ in _session().env}
    assert not keys.intersection(
        {
            "DATABASE_URL",
            "REDIS_URL",
            "S3_SECRET_KEY",
            "S3_ACCESS_KEY",
            "JWT_SECRET",
            "SECRETS_ENCRYPTION_KEY",
            "OPENROUTER_API_KEY",
            "CELERY_BROKER_URL",
            "DOCKER_HOST",
        }
    )


def test_inputs_remain_explicit_read_only_bytes() -> None:
    value = StagedInput(ref_id=uuid4(), dest_path="/inputs/data.csv", data=b"x")
    assert value.read_only is True


@pytest.mark.parametrize("runtime", ["runc", "runsc"])
def test_runtime_is_an_explicit_runner_setting(runtime: str) -> None:
    assert build_container_flags(_session(runtime=runtime)).runtime == runtime


def test_non_local_enabled_sandbox_requires_gvisor() -> None:
    common: dict[str, object] = {
        "ENVIRONMENT": "production",
        "JWT_SECRET": "production-secret-that-is-not-the-dev-default",
        "SECRETS_ENCRYPTION_KEY": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        "SANDBOX_ENABLED": True,
        # ADR-0019 §1 (#452): a non-local environment refuses http OAuth
        # callback/return URLs — part of the minimal production boot env.
        "CONNECTOR_OAUTH_REDIRECT_BASE_URL": "https://api.example.com",
        "CONNECTOR_OAUTH_FRONTEND_RETURN_URL": "https://app.example.com/sources",
        # ADR-0019 §1 (#453): ... and a Google client registration (blank is
        # refused outside local) — also part of the minimal production env.
        "GDRIVE_OAUTH_CLIENT_ID": "prod-google-client-id",
        "GDRIVE_OAUTH_CLIENT_SECRET": "prod-google-client-secret",
        # ADR-0013 §3: an enabled sandbox outside local dev runs a DIGEST-pinned
        # execution image, so the minimal production env carries one (the tag-only
        # default is refused — see test_sandbox_config).
        "SANDBOX_IMAGE": "lumen-sandbox-exec@sha256:" + "a" * 64,
    }
    with pytest.raises(ValidationError, match="runsc"):
        Settings(**common, SANDBOX_RUNTIME="runc")  # type: ignore[arg-type]

    assert (
        Settings(**common, SANDBOX_RUNTIME="runsc").sandbox_runtime  # type: ignore[arg-type]
        == "runsc"
    )
