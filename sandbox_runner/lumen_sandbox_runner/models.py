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
    read_only_rootfs: Literal[False] = False
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
    cpus: None = None
    memory_bytes: None = None
    pids_limit: None = None
    wall_clock_seconds: None = None
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
    packages: list[PackageRequirement] = Field(default_factory=list, max_length=50)
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
