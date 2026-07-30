"""Offline enforcement-wiring tests for contained root sessions (ADR-0020)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.sandbox.runner import build_container_flags
from app.sandbox.service import session_resource_bounds
from app.sandbox.spec import SandboxSessionSpec, StagedInput
from tests._sandbox_helpers import sandbox_settings


def _limits_for(settings: Settings) -> tuple[float | None, int | None, int | None]:
    """The whole backend chain for a session bound: config → spec → container flags."""
    cpus, memory_bytes, pids_limit = session_resource_bounds(settings)
    flags = build_container_flags(
        _session(cpus=cpus, memory_bytes=memory_bytes, pids_limit=pids_limit)
    )
    return (flags.cpus, flags.memory_bytes, flags.pids_limit)


def _session(
    *,
    runtime: str = "runsc",
    output_bytes_cap: int | None = None,
    cpus: float | None = None,
    memory_bytes: int | None = None,
    pids_limit: int | None = None,
) -> SandboxSessionSpec:
    return SandboxSessionSpec(
        sandbox_session_id=uuid4(),
        generation=1,
        image="python@sha256:abc",
        runtime=runtime,
        output_bytes_cap=output_bytes_cap,
        cpus=cpus,
        memory_bytes=memory_bytes,
        pids_limit=pids_limit,
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


def test_the_shipped_default_sends_no_cpu_memory_or_pid_bound() -> None:
    """ADR-0020's posture is the default, and it is stated here rather than implied.

    Its Consequences section is explicit that there is no automatic protection against
    infinite loops, fork bombs, disk growth or resource monopolisation, and that this
    is a deliberate sponsor decision. So the *absence* is asserted: a change that
    started bounding sessions silently would fail this test and have to argue for it.
    """
    limits = _limits_for(sandbox_settings())

    assert limits == (None, None, None)


def test_a_deploy_may_opt_into_bounding_the_session_container() -> None:
    """The gap this closes: the risk was documented but not configurable.

    The runner's wire schema typed cpu/memory/PID ``None``, so a deployer who was not
    willing to let one ``run_python`` call exhaust host RAM could not even ask for a
    bound. With ``SANDBOX_SESSION_LIMITS_ENABLED`` the ADR-0013 numbers a deploy
    already configures are carried down to the engine flags.
    """
    settings = sandbox_settings(
        SANDBOX_SESSION_LIMITS_ENABLED="true",
        SANDBOX_CPUS="1.5",
        SANDBOX_MEMORY_BYTES=str(256 * 1024 * 1024),
        SANDBOX_PIDS_LIMIT="64",
    )

    assert _limits_for(settings) == (1.5, 256 * 1024 * 1024, 64)
    # Still no wall clock: nothing enforces one, so nothing claims to.
    assert build_container_flags(_session()).wall_clock_seconds is None


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
