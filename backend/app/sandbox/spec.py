"""Reusable sandbox session/run domain value objects (ADR-0020, #457).

Pure, frozen dataclasses that describe **what to run** (:class:`RunSpec` — the code,
package requirements, and staged read-only inputs) and **what came
back** (:class:`RunResult` — status, captured output, produced files, timing,
resource usage). The ``sandbox/`` module exposes *these* domain types to its
``services`` caller — never the ``sandbox-runner``'s wire objects or Docker's
container objects (ADR-0004 boundary rule).

Legacy ADR-0013 policy value objects remain for source compatibility. ADR-0020's
active enforcement is the fixed runner mapping: network mode ``none``, no host mounts,
all capabilities dropped, contained UID 0, and no automatic execution limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from app.domain.entities import CodeRunStatus, ResourceUsage

# The metadata IP an SSRF/escape attempt targets for cloud credentials. It is
# **never** allowlistable (G4) — the policy strips it from any egress allowlist.
METADATA_IP = "169.254.169.254"


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Legacy ADR-0013 limits retained only for reading older callers/tests.

    These values are not populated or passed to the ADR-0020 reusable runner.
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
    """Legacy per-run policy value retained for source compatibility.

    ADR-0020 ignores its egress/package booleans: model code is always offline and
    admitted packages are acquired through the runner before tenant bytes are staged.
    ``runtime`` remains meaningful on :class:`SandboxSessionSpec`.
    """

    egress: EgressPolicy = field(default_factory=EgressPolicy)
    allow_package_install: bool = False
    runtime: str = "runc"


@dataclass(frozen=True, slots=True)
class SandboxSessionSpec:
    """The opaque reusable-container identity passed to the runner (ADR-0020).

    ``output_bytes_cap`` is the one limit ADR-0020 does enforce, and it protects the
    RUNNER rather than the run: collected output files are read into the runner's
    memory, and the runner is the single holder of the Docker socket, so an unbounded
    collection let one chat turn OOM the process every tenant's code execution depends
    on. ``None`` leaves the runner's own ceiling in force.

    ``cpus``/``memory_bytes``/``pids_limit`` bound the session CONTAINER, and they are
    ``None`` by default because ADR-0020 ships unbounded on purpose (its Consequences
    section: no automatic protection against infinite loops, fork bombs or disk growth,
    explicit cancel being the recovery path). A deploy unwilling to accept that sets
    ``SANDBOX_SESSION_LIMITS_ENABLED`` and these carry its ``SANDBOX_CPUS`` /
    ``SANDBOX_MEMORY_BYTES`` / ``SANDBOX_PIDS_LIMIT`` down to the engine flags.
    """

    sandbox_session_id: UUID
    generation: int
    image: str
    runtime: str
    output_bytes_cap: int | None = None
    cpus: float | None = None
    memory_bytes: int | None = None
    pids_limit: int | None = None
    env: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("sandbox generation must be >= 1")
        if self.output_bytes_cap is not None and self.output_bytes_cap < 1:
            raise ValueError("sandbox output_bytes_cap must be >= 1 when set")
        # A non-positive bound is not "no bound": it is a container the engine refuses
        # to create, and discovering that at session start would take code execution
        # down for a whole deploy. Rejected here, where the operator can read it.
        if self.cpus is not None and self.cpus <= 0:
            raise ValueError("sandbox cpus must be > 0 when set")
        if self.memory_bytes is not None and self.memory_bytes < 1:
            raise ValueError("sandbox memory_bytes must be >= 1 when set")
        if self.pids_limit is not None and self.pids_limit < 1:
            raise ValueError("sandbox pids_limit must be >= 1 when set")


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
    """One execution inside a reusable sandbox generation (ADR-0020).

    Carries the exact ``code`` to execute, the ``inputs`` staged read-only, the
    optional compatibility policy values. This is the domain shape the
    ``SandboxService`` builds and hands to a :class:`SandboxRunner`; the runner client
    maps only ADR-0020 fields to the runner wire form.
    """

    code: str
    execution_id: UUID = field(default_factory=uuid4)
    packages: tuple[str, ...] = ()
    # Kept optional for source compatibility with historical callers. ADR-0020
    # never populates or enforces automatic run limits.
    limits: RunLimits | None = None
    policy: SandboxPolicy = field(default_factory=SandboxPolicy)
    inputs: tuple[StagedInput, ...] = ()
    # Minimal, curated env handed to the run. NEVER app secrets / DB creds (G5) —
    # the service builds this and the runner passes ONLY this, dropping the host env.
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class OutputFile:
    """One file the code wrote to the designated output dir, collected by the runner.

    Persisted as a tenant-scoped artifact via CC-B (#208) by the service. ``data`` is
    the collected bytes; ``filename`` and
    ``content_type`` drive the artifact's metadata/allowlist check.
    """

    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class RunResult:
    """A completed sandbox run's outcome (runner → worker, ADR-0020 §4).

    ``status`` is the terminal :class:`CodeRunStatus` (``succeeded``/``failed``/
    historical ``timeout``/explicit ``killed``); ``stdout``/``stderr`` are captured;
    ``output_files`` are the collected files (→ artifacts, CC-B). ``image_digest``
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
