"""Sandbox run spec + result — the domain value objects (ADR-0013 §4, #230).

Pure, frozen dataclasses that describe **what to run** (:class:`RunSpec` — the code,
staged read-only inputs, resource limits, egress/package policy) and **what came
back** (:class:`RunResult` — status, captured output, produced files, timing,
resource usage). The ``sandbox/`` module exposes *these* domain types to its
``services`` caller — never the ``sandbox-runner``'s wire objects or Docker's
container objects (ADR-0004 boundary rule).

The isolation guarantees (G1–G8, ADR-0013 §5) are expressed here as **explicit,
inspectable enforcement fields** so the offline tests can assert the wiring is
correct (network mode is ``none``, no host mounts, caps dropped, limits present)
even where the true kernel-level behaviour can only be proven against a live
runner. Deny-by-default is the default: a freshly constructed :class:`SandboxPolicy`
denies all egress and installs no packages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from app.domain.entities import CodeRunStatus, ResourceUsage

# The metadata IP an SSRF/escape attempt targets for cloud credentials. It is
# **never** allowlistable (G4) — the policy strips it from any egress allowlist.
METADATA_IP = "169.254.169.254"


@dataclass(frozen=True, slots=True)
class RunLimits:
    """The per-run resource caps enforced by the sandbox (ADR-0013 §2, G6/G7).

    Every field is a hard ceiling the runner passes to the container engine (CPU
    quota, memory → OOM-kill, pids → fork-bomb cap) or enforces itself (wall-clock
    → SIGKILL, output bytes → truncate/fail). All must be **positive**; the config
    layer validates them fail-fast so a run can never be launched unbounded.
    """

    cpus: float
    memory_bytes: int
    pids: int
    wall_clock_seconds: int
    output_bytes_cap: int


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """The network egress decision for a run (deny-by-default, ADR-0013 §2/§5, G2–G4).

    ``allowed`` is False by default: a run gets ``--network none`` and can reach
    nothing — not the internet, not internal services, not the metadata IP (G2/G3/G4).
    Only an admin may set ``allowed=True`` with an explicit ``allowlist`` of host:port
    targets; the metadata IP is stripped from any allowlist and can never be reached
    (G4). ``network_mode`` is the concrete engine flag the runner applies —
    ``"none"`` unless an allowlist opens a constrained path.
    """

    allowed: bool = False
    allowlist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # G4: the metadata IP is never allowlistable, even under an admin allowlist.
        cleaned = tuple(t for t in self.allowlist if METADATA_IP not in t)
        object.__setattr__(self, "allowlist", cleaned)

    @property
    def network_mode(self) -> str:
        """The container network mode the runner enforces (``none`` = fully isolated)."""
        if self.allowed and self.allowlist:
            return "restricted"
        return "none"


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """The full run policy: egress + package + runtime (ADR-0013 §3/§5, deny-by-default).

    ``egress`` denies all network by default (G2–G4). ``allow_package_install`` is
    False: a run gets exactly the curated, pinned base image's libraries — no
    arbitrary internet ``pip install`` (ADR-0013 §3). ``runtime`` names the OCI
    runtime the runner uses (``runc`` = hardened Docker baseline; ``runsc`` = the
    recommended gVisor production hardening — a config swap, no code change).
    """

    egress: EgressPolicy = field(default_factory=EgressPolicy)
    allow_package_install: bool = False
    runtime: str = "runc"


@dataclass(frozen=True, slots=True)
class StagedInput:
    """One artifact/document staged **read-only** into a run (ADR-0013 §4).

    The code sees the bytes at ``dest_path`` inside the run's read-only input dir and
    **cannot mutate the source** (the source object store is never writable from the
    sandbox). ``ref_id`` is the artifact/document id the bytes came from (recorded for
    reproducibility); ``read_only`` is always True — a settable field only so the
    invariant is explicit and testable.
    """

    ref_id: UUID
    dest_path: str
    data: bytes
    read_only: bool = True


@dataclass(frozen=True, slots=True)
class RunSpec:
    """A complete sandbox run request (worker → runner, ADR-0013 §4 ``POST /runs``).

    Carries the exact ``code`` to execute, the ``inputs`` staged read-only, the
    ``limits`` (G6/G7), and the ``policy`` (egress/package/runtime, G2–G4). This is
    the domain shape the ``SandboxService`` builds and hands to a :class:`SandboxRunner`;
    the runner client maps it to the runner's wire form.
    """

    code: str
    limits: RunLimits
    policy: SandboxPolicy = field(default_factory=SandboxPolicy)
    inputs: tuple[StagedInput, ...] = ()
    # Minimal, curated env handed to the run. NEVER app secrets / DB creds (G5) —
    # the service builds this and the runner passes ONLY this, dropping the host env.
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class OutputFile:
    """One file the code wrote to the designated output dir, collected by the runner.

    Persisted as a tenant-scoped artifact via CC-B (#208) by the service. ``data`` is
    the collected bytes (already output-size-capped, G7); ``filename`` and
    ``content_type`` drive the artifact's metadata/allowlist check.
    """

    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class RunResult:
    """A completed sandbox run's outcome (runner → worker, ADR-0013 §4).

    ``status`` is the terminal :class:`CodeRunStatus` (``succeeded``/``failed``/
    ``timeout``/``killed``); ``stdout``/``stderr`` are captured + output-size-capped
    (G7); ``output_files`` are the collected files (→ artifacts, CC-B). ``image_digest``
    is the pinned base image actually used (reproducibility, E3-7). ``resource_usage``
    is best-effort measured consumption.
    """

    status: CodeRunStatus
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    output_files: tuple[OutputFile, ...] = ()
    image_digest: str | None = None
    resource_usage: ResourceUsage | None = None
