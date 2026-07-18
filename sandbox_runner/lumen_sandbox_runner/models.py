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
    runtime: Literal["runc", "runsc"] = "runc"
    cpus: None = None
    memory_bytes: None = None
    pids_limit: None = None
    wall_clock_seconds: None = None
    output_bytes_cap: None = None


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
