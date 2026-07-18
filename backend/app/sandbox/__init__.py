"""Reusable sandboxed code execution — the owning module (ADR-0020, issue #457).

The single owning application module for **sandboxed code execution** (ADR-0020
boundary row proposed for AGENTS.md §6). It is the **only** caller of the
``sandbox-runner`` internal API (via :class:`~app.sandbox.runner.HttpSandboxRunner`);
the runner service is the **only** holder of container-engine / Docker-socket access.
No other module orchestrates code execution or talks to the runner.

The package exposes **domain types only** (ADR-0004): the run spec/result value
objects (:mod:`app.sandbox.spec`), the runner protocol, and the execution/read
services. Docker's or the runner's wire objects never escape this boundary.
"""

from __future__ import annotations

from app.sandbox.runner import (
    ContainerFlags,
    HttpSandboxRunner,
    SandboxRunner,
    build_container_flags,
)
from app.sandbox.service import SandboxReadService, SandboxService, SandboxSessionService
from app.sandbox.spec import (
    METADATA_IP,
    EgressPolicy,
    OutputFile,
    RunLimits,
    RunResult,
    RunSpec,
    SandboxPolicy,
    SandboxSessionSpec,
    StagedInput,
)

__all__ = [
    "METADATA_IP",
    "ContainerFlags",
    "EgressPolicy",
    "HttpSandboxRunner",
    "OutputFile",
    "RunLimits",
    "RunResult",
    "RunSpec",
    "SandboxPolicy",
    "SandboxReadService",
    "SandboxRunner",
    "SandboxService",
    "SandboxSessionService",
    "SandboxSessionSpec",
    "StagedInput",
    "build_container_flags",
]
