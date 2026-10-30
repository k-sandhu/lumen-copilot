"""Validated internal wire types; security-critical fields are fixed by schema."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, field_validator

PackageRequirement = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
]


class ContainerPolicy(BaseModel):
    model_config = {"extra": "forbid"}

    network_mode: Literal["none"] = "none"
    binds: list[str] = Field(default_factory=list, max_length=0)
    read_only_rootfs: bool = False
    user: Literal["0:0"] = "0:0"
    cap_drop: list[Literal["ALL"]] = Field(
        default_factory=lambda: ["ALL"], min_length=1, max_length=1
    )
    security_opt: list[Literal["no-new-privileges:true"]] = Field(
        default_factory=lambda: ["no-new-privileges:true"], min_length=1, max_length=1
    )
    # NOT the runtime that gets used: the OCI runtime is read from THIS service's own
    # ``SANDBOX_RUNTIME`` (see ``DockerSandboxEngine._configured_runtime``), because a
    # caller-selectable runtime would let anyone who reaches the internal API run one
    # session under plain ``runc`` on a deploy whose safety argument rests on gVisor.
    # This field is what the caller BELIEVES is in force; a disagreement is refused
    # rather than silently overridden, so a half-restarted deploy is loud.
    runtime: Literal["runc", "runsc"] = "runc"
    # Deployment ceilings arrive from the backend; explicit null supports the
    # emergency container-bound opt-out. Defaults are bounded by backend config.
    cpus: float | None = Field(default=None, ge=1e-9, allow_inf_nan=False)
    # Docker's own floor for ``--memory`` is 6 MiB; a smaller value is a container the
    # daemon refuses to create, so it is rejected here where the error is readable.
    memory_bytes: int | None = Field(default=None, ge=6 * 1024 * 1024)
    pids_limit: int | None = Field(default=None, ge=1)
    # Enforced since #589: the engine destroys the container when an
    # execution passes this deadline (per-execution value rides on
    # ``ExecuteRequest``; this session-level value is the fallback).
    wall_clock_seconds: int | None = Field(default=None, ge=1)
    #: Scratch bound for the session workspace, applied as a tmpfs size on
    #: ``/workspace`` (#589). Narrowing only.
    scratch_bytes: int | None = Field(default=None, ge=1)
    # The one policy value a caller may choose, because it only ever NARROWS what this
    # process will hold in memory: the runner clamps it to its own ceiling and applies
    # that ceiling when the field is absent. It was typed ``None`` and read by nothing,
    # so output collection was unbounded — one execution's files could OOM the single
    # Docker-socket holder and take code execution down for every tenant.
    output_bytes_cap: int | None = Field(default=None, ge=1)


class EnsureSessionRequest(BaseModel):
    model_config = {"extra": "forbid"}

    generation: int = Field(ge=1)
    image: str = Field(min_length=1, max_length=500)
    env: dict[str, str] = Field(default_factory=dict)
    container: ContainerPolicy = Field(default_factory=ContainerPolicy)

    @field_validator("env")
    @classmethod
    def env_is_curated(cls, value: dict[str, str]) -> dict[str, str]:
        allowed = {"HOME", "PYTHONUNBUFFERED", "MPLBACKEND", "LANG"}
        if not set(value).issubset(allowed):
            raise ValueError("session env contains a non-curated key")
        return value


class StagedInputRequest(BaseModel):
    model_config = {"extra": "forbid"}

    ref_id: UUID
    dest_path: str = Field(min_length=1, max_length=1000)
    data_b64: str
    read_only: Literal[True] = True


class ExecuteRequest(BaseModel):
    model_config = {"extra": "forbid"}

    generation: int = Field(ge=1)
    execution_id: UUID
    code: str = Field(min_length=1)
    #: Per-execution wall clock (#589); at the deadline the engine destroys
    #: the container and reports ``timeout`` / ``sandbox_limit_wall_clock``.
    wall_clock_seconds: int | None = Field(default=None, ge=1)
    deadline_unix: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    packages: list[PackageRequirement] = Field(default_factory=list, max_length=50)
    #: The tenant's deny list, so the RESOLVED dependency tree can be checked against
    #: it (#509). The backend applies it to the top-level request, but only the runner
    #: ever sees what pip actually resolves — and `pip install` takes the whole
    #: wheelhouse, so a denied distribution could arrive as a dependency of an allowed
    #: one. Deny `urllib3`, allow `requests`, and `urllib3` was installed anyway.
    #:
    #: Sent rather than configured on the runner because it is TENANT policy and the
    #: runner is tenant-agnostic. Empty means "no denials", which is the default state
    #: and must not be confused with "the caller forgot" — the backend always sends the
    #: resolved policy, so an empty list is a real answer.
    denied_packages: list[PackageRequirement] = Field(default_factory=list, max_length=200)
    env: dict[str, str] = Field(default_factory=dict)
    inputs: list[StagedInputRequest] = Field(default_factory=list)

    @field_validator("env")
    @classmethod
    def run_env_is_curated(cls, value: dict[str, str]) -> dict[str, str]:
        if not set(value).issubset({"LUMEN_OUTPUT_DIR"}):
            raise ValueError("execution env contains a non-curated key")
        return value


class CancelRequest(BaseModel):
    model_config = {"extra": "forbid"}

    generation: int = Field(ge=1)
    execution_id: UUID
