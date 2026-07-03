"""Sandbox isolation tests — the enforced-wiring negatives (ADR-0013 §5, G1–G8, #230).

**The isolation negative tests are the core deliverable of #230 — a bypass is a
blocking defect** (the same bar as the SSRF chokepoint / retrieval permission filter).
The true kernel-level isolation (an actual escape attempt) can only be *proven* live
against the compose ``sandbox-runner``; these offline tests assert the **enforcement
wiring** — that the run spec the worker hands the runner is configured to deny every
avenue G1–G8 — so a regression in the *configuration* fails here before the live
end-to-end ever runs (ADR-0013 §5 testing note):

* **G1 host filesystem unreachable** — no host bind-mounts; read-only rootfs; a
  size-capped tmpfs scratch is the ONLY writable area.
* **G2/G3/G4 egress denied** — ``--network none`` by default (no internet, no internal
  services, no metadata IP); the metadata IP is NEVER allowlistable even under an
  admin allowlist.
* **G5 no secret / env leakage** — the run receives ONLY a minimal curated env; no app
  secret, DB URL, or object-store credential is ever in the spec.
* **G6 cpu/mem/pids/wall-clock enforced** — every resource cap is present and positive.
* **G7 output-size cap** — the captured-output cap is present.
* **Runtime tiers** — ``runc`` baseline / ``runsc`` gVisor is a config swap, no code
  change: the built flags carry the configured runtime verbatim.
"""

from __future__ import annotations

import pytest

from app.sandbox.runner import build_container_flags
from app.sandbox.spec import (
    METADATA_IP,
    EgressPolicy,
    RunLimits,
    RunSpec,
    SandboxPolicy,
    StagedInput,
)

_LIMITS = RunLimits(
    cpus=1.0,
    memory_bytes=512 * 1024 * 1024,
    pids=128,
    wall_clock_seconds=30,
    output_bytes_cap=1024 * 1024,
)
_SCRATCH = 256 * 1024 * 1024


def _spec(**overrides: object) -> RunSpec:
    base: dict[str, object] = {
        "code": "print('hi')",
        "limits": _LIMITS,
    }
    base.update(overrides)
    return RunSpec(**base)  # type: ignore[arg-type]


# --- G1: host filesystem unreachable ----------------------------------------


def test_g1_no_host_bind_mounts() -> None:
    """G1: the run has NO host bind-mounts — the host filesystem is unreachable."""
    flags = build_container_flags(_spec(), tmpfs_scratch_bytes=_SCRATCH)
    assert flags.bind_mounts == ()


def test_g1_read_only_rootfs_with_tmpfs_scratch() -> None:
    """G1: the root filesystem is read-only; the ONLY writable area is a capped tmpfs."""
    flags = build_container_flags(_spec(), tmpfs_scratch_bytes=_SCRATCH)
    assert flags.read_only_rootfs is True
    assert flags.tmpfs_scratch_bytes == _SCRATCH
    assert flags.tmpfs_scratch_bytes > 0


def test_g1_inputs_are_staged_read_only() -> None:
    """Staged inputs are read-only — the code cannot mutate the source (ADR-0013 §4)."""
    from uuid import uuid4

    staged = StagedInput(ref_id=uuid4(), dest_path="/inputs/data.csv", data=b"x")
    assert staged.read_only is True


# --- G2/G3/G4: egress denied by default -------------------------------------


def test_g2_g3_network_none_by_default() -> None:
    """G2/G3: with no egress policy the run gets network mode ``none`` — no egress at all."""
    flags = build_container_flags(_spec(), tmpfs_scratch_bytes=_SCRATCH)
    assert flags.network_mode == "none"
    assert flags.egress_allowlist == ()


def test_default_policy_denies_egress() -> None:
    """A freshly constructed policy denies all egress (deny-by-default, ADR-0013 §5)."""
    policy = SandboxPolicy()
    assert policy.egress.allowed is False
    assert policy.egress.network_mode == "none"


def test_g4_metadata_ip_never_allowlistable() -> None:
    """G4: the metadata IP is stripped from ANY egress allowlist — never reachable.

    Even when an admin configures an allowlist (the only way to open egress at all),
    ``169.254.169.254`` can never be a target: the :class:`EgressPolicy` strips it at
    construction and :func:`build_container_flags` strips it again (belt-and-braces).
    """
    egress = EgressPolicy(allowed=True, allowlist=(f"{METADATA_IP}:80", "api.example.com:443"))
    # Stripped at the policy boundary.
    assert all(METADATA_IP not in t for t in egress.allowlist)
    assert "api.example.com:443" in egress.allowlist

    flags = build_container_flags(
        _spec(policy=SandboxPolicy(egress=egress)), tmpfs_scratch_bytes=_SCRATCH
    )
    # And stripped again in the built flags handed to the runner.
    assert all(METADATA_IP not in t for t in flags.egress_allowlist)
    assert flags.network_mode == "restricted"  # an allowlist opens a constrained path


def test_g4_metadata_ip_alone_yields_empty_allowlist() -> None:
    """An allowlist of ONLY the metadata IP collapses to empty — no egress opens."""
    egress = EgressPolicy(allowed=True, allowlist=(f"http://{METADATA_IP}/latest/meta",))
    assert egress.allowlist == ()


# --- G5: no secret / env leakage --------------------------------------------


def test_g5_spec_env_carries_no_secrets_by_default() -> None:
    """G5: a bare run spec carries no env at all — the service supplies only curated keys."""
    spec = _spec()
    assert spec.env == ()


def test_g5_curated_env_has_no_app_secrets() -> None:
    """G5: the service's curated env contains no secret / DB / object-store material.

    Builds the exact spec the service hands the runner and asserts none of the app's
    sensitive config names/values leak into the run's environment.
    """
    from tests._sandbox_helpers import build_service_spec

    spec = build_service_spec(code="print(1)")
    keys = {k.upper() for k, _ in spec.env}
    values = " ".join(v for _, v in spec.env).lower()
    forbidden_keys = {
        "DATABASE_URL",
        "REDIS_URL",
        "S3_SECRET_KEY",
        "S3_ACCESS_KEY",
        "JWT_SECRET",
        "SECRETS_ENCRYPTION_KEY",
        "OPENROUTER_API_KEY",
        "CELERY_BROKER_URL",
    }
    assert not (keys & forbidden_keys)
    # No obvious secret material by value either (the dev creds used in tests).
    for marker in ("postgresql", "redis://", "lumen_local_dev", "secret"):
        assert marker not in values


# --- G6: cpu / mem / pids / wall-clock enforced -----------------------------


def test_g6_all_resource_caps_present_and_positive() -> None:
    """G6: cpu, memory (OOM), pids (fork-bomb), and wall-clock caps are all set + positive."""
    flags = build_container_flags(_spec(), tmpfs_scratch_bytes=_SCRATCH)
    assert flags.cpus == _LIMITS.cpus > 0
    assert flags.memory_bytes == _LIMITS.memory_bytes > 0
    assert flags.pids_limit == _LIMITS.pids > 0
    assert flags.wall_clock_seconds == _LIMITS.wall_clock_seconds > 0


def test_g6_caps_dropped_and_non_root_no_new_privileges() -> None:
    """ADR-0013 §2: ALL caps dropped, non-root, no-new-privileges, a seccomp profile."""
    flags = build_container_flags(_spec(), tmpfs_scratch_bytes=_SCRATCH)
    assert flags.cap_drop == ("ALL",)
    assert flags.run_as_non_root is True
    assert flags.no_new_privileges is True
    assert flags.seccomp_profile != ""


# --- G7: output-size cap ----------------------------------------------------


def test_g7_output_size_cap_present() -> None:
    """G7: the captured-output size cap is present and positive."""
    flags = build_container_flags(_spec(), tmpfs_scratch_bytes=_SCRATCH)
    assert flags.output_bytes_cap == _LIMITS.output_bytes_cap > 0


# --- Runtime tiers (runc baseline / runsc gVisor prod hardening) ------------


@pytest.mark.parametrize("runtime", ["runc", "runsc"])
def test_runtime_tier_is_a_config_swap(runtime: str) -> None:
    """The OCI runtime rides through verbatim — gVisor is config, not a code change."""
    flags = build_container_flags(
        _spec(policy=SandboxPolicy(runtime=runtime)), tmpfs_scratch_bytes=_SCRATCH
    )
    assert flags.runtime == runtime
