"""Offline enforcement-wiring tests for contained root sessions (ADR-0020)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.sandbox.runner import build_container_flags
from app.sandbox.spec import SandboxSessionSpec, StagedInput


def _session(*, runtime: str = "runsc") -> SandboxSessionSpec:
    return SandboxSessionSpec(
        sandbox_session_id=uuid4(),
        generation=1,
        image="python@sha256:abc",
        runtime=runtime,
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


def test_no_automatic_time_resource_pid_or_output_limits() -> None:
    flags = build_container_flags(_session())
    assert flags.wall_clock_seconds is None
    assert flags.cpus is None
    assert flags.memory_bytes is None
    assert flags.pids_limit is None
    assert flags.output_bytes_cap is None


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
    }
    with pytest.raises(ValidationError, match="runsc"):
        Settings(**common, SANDBOX_RUNTIME="runc")  # type: ignore[arg-type]

    assert (
        Settings(**common, SANDBOX_RUNTIME="runsc").sandbox_runtime  # type: ignore[arg-type]
        == "runsc"
    )
