"""The sandbox runner client + the enforced isolation flags (ADR-0013 §1/§2, #230).

The ``sandbox-runner`` compose service owns **all** container lifecycle and is the
**only** holder of container-engine / Docker-socket access (ADR-0013 §1); the Celery
worker calls it over an **internal HTTP API** on the compose network, never published
to a host port, and **never mounts the Docker socket itself** (mounting the socket
into the app/worker is equivalent to host root — explicitly forbidden). This module
is the one client of that API.

Two things live here:

* :func:`build_container_flags` — the pure, inspectable translation of a
  :class:`~app.sandbox.spec.RunSpec` into the concrete per-run container hardening
  flags (network mode, mounts, dropped caps, seccomp, non-root, read-only rootfs,
  cpu/mem/pids/timeout/output caps). This is the enforcement wiring the **offline
  isolation tests assert** (network mode = ``none``, mounts = none, caps dropped,
  limits present) — the guarantees whose true kernel-level behaviour is proven live
  against the runner but whose *configuration* is proven offline here (ADR-0013 §5
  testing note).
* :class:`HttpSandboxRunner` — the live client that POSTs a run to the runner and
  polls it to completion, mapping the runner's JSON back to a domain
  :class:`~app.sandbox.spec.RunResult`. A :class:`FakeSandboxRunner` (in tests) or
  this implement the :class:`SandboxRunner` protocol interchangeably, so the service
  is testable offline and the true isolation is exercised against the compose runner.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.errors import DependencyError
from app.core.logging import get_logger
from app.domain.entities import CodeRunStatus, ResourceUsage
from app.sandbox.spec import METADATA_IP, OutputFile, RunResult, RunSpec

log = get_logger(__name__)

# The Linux capability policy: drop ALL, add none (ADR-0013 §2). A container that
# needs no capability cannot escalate through one.
_CAP_DROP = ("ALL",)


@dataclass(frozen=True, slots=True)
class ContainerFlags:
    """The concrete per-run container hardening the runner applies (ADR-0013 §2).

    Every field maps to a container-engine flag; the offline isolation tests assert
    the load-bearing ones (``network_mode == "none"``, ``bind_mounts == ()``,
    ``read_only_rootfs``, ``cap_drop == ("ALL",)``, ``no_new_privileges``, non-root,
    and the cpu/mem/pids/timeout/output caps) so a regression in the *wiring* is a
    test failure even before the live end-to-end runs.
    """

    # G2/G3/G4: no network by default. "none" = the container has no network device
    # at all — no route to the internet, internal services, or the metadata IP.
    network_mode: str
    # G1: NO host bind-mounts. The only writable area is an in-container tmpfs scratch.
    bind_mounts: tuple[str, ...]
    # G1: read-only root filesystem; the run's CWD is a size-capped tmpfs.
    read_only_rootfs: bool
    tmpfs_scratch_bytes: int
    # Non-root + no privilege escalation (ADR-0013 §2).
    run_as_non_root: bool
    no_new_privileges: bool
    cap_drop: tuple[str, ...]
    seccomp_profile: str
    # G6: cpu/mem(OOM)/pids caps + wall-clock timeout (SIGKILL on expiry).
    cpus: float
    memory_bytes: int
    pids_limit: int
    wall_clock_seconds: int
    # G7: captured-output size cap.
    output_bytes_cap: int
    # The OCI runtime (runc = hardened Docker baseline; runsc = gVisor prod hardening).
    runtime: str
    # G4: the metadata IP is asserted-blocked even when an allowlist is configured.
    egress_allowlist: tuple[str, ...] = ()


def build_container_flags(spec: RunSpec, *, tmpfs_scratch_bytes: int) -> ContainerFlags:
    """Translate a :class:`RunSpec` into the enforced container flags (ADR-0013 §2/§5).

    Pure and deterministic so the offline isolation tests can assert the wiring
    directly. Deny-by-default is structural: the network mode comes from the policy's
    :class:`EgressPolicy` (``none`` unless an admin allowlist opens a constrained
    path), the metadata IP is never in the allowlist (G4), there are **no** host bind
    mounts (G1), the rootfs is read-only with a tmpfs scratch (G1), all caps are
    dropped (ADR-0013 §2), and the cpu/mem/pids/timeout/output caps come straight from
    the run's :class:`RunLimits` (G6/G7).
    """
    egress = spec.policy.egress
    # G4 (belt-and-braces): even if a caller hand-built an allowlist, strip the
    # metadata IP here too — the runner must never receive it as reachable.
    allowlist = tuple(t for t in egress.allowlist if METADATA_IP not in t)
    return ContainerFlags(
        network_mode=egress.network_mode,
        bind_mounts=(),  # G1: never a host path
        read_only_rootfs=True,  # G1
        tmpfs_scratch_bytes=tmpfs_scratch_bytes,  # G1: the only writable area
        run_as_non_root=True,
        no_new_privileges=True,
        cap_drop=_CAP_DROP,
        seccomp_profile="default",
        cpus=spec.limits.cpus,  # G6
        memory_bytes=spec.limits.memory_bytes,  # G6 (OOM-kill)
        pids_limit=spec.limits.pids,  # G6 (fork-bomb cap)
        wall_clock_seconds=spec.limits.wall_clock_seconds,  # G6 (SIGKILL)
        output_bytes_cap=spec.limits.output_bytes_cap,  # G7
        runtime=spec.policy.runtime,
        egress_allowlist=allowlist,
    )


class SandboxRunner(Protocol):
    """The out-of-process sandbox boundary (ADR-0013 §1) the service depends on.

    One method: run a :class:`RunSpec` to completion and return a domain
    :class:`RunResult`. The live :class:`HttpSandboxRunner` drives the compose
    ``sandbox-runner``; a fake implements the same protocol for the offline tests, so
    the ``SandboxService`` orchestration + isolation-wiring is fully testable without
    a container engine, and the true kernel-level isolation is exercised live.
    """

    async def run(self, spec: RunSpec, *, tmpfs_scratch_bytes: int) -> RunResult:
        """Execute ``spec`` in a fresh, single-use sandbox and return the outcome."""
        ...


def _map_status(raw: object) -> CodeRunStatus:
    """Map the runner's status string to a domain :class:`CodeRunStatus` (fail-closed).

    An unknown/absent status is treated as ``failed`` (never ``succeeded``) so a
    malformed runner response can never make a run look successful.
    """
    try:
        return CodeRunStatus(str(raw))
    except ValueError:
        return CodeRunStatus.FAILED


class HttpSandboxRunner:
    """The live client for the compose ``sandbox-runner`` internal HTTP API (ADR-0013 §1).

    POSTs a run spec (with the enforced container flags) to ``{base_url}/runs`` and
    polls ``{base_url}/runs/{id}`` until the run reaches a terminal status or the
    wall-clock budget elapses. Maps the runner's JSON to a domain
    :class:`RunResult`, decoding any collected output files. A runner that is
    unreachable / errors surfaces as a :class:`DependencyError` (the service maps a
    failure to a terminal ``failed`` run — a run never ends in silence, ADR-0013 §5).

    The worker holds **no** Docker socket; this HTTP hop to the dedicated runner is
    the entire container-engine surface (the socket lives in exactly one service).
    """

    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._connect_timeout = connect_timeout_seconds
        self._poll_interval = poll_interval_seconds
        # An injectable transport for offline tests (an ``httpx.MockTransport``); the
        # live client uses the default network transport against the compose runner.
        self._transport = transport

    async def run(self, spec: RunSpec, *, tmpfs_scratch_bytes: int) -> RunResult:
        flags = build_container_flags(spec, tmpfs_scratch_bytes=tmpfs_scratch_bytes)
        payload = self._to_wire(spec, flags)
        # The overall wait is bounded by the run's own wall-clock budget + a small
        # margin so a stuck runner never hangs the worker (the runner also SIGKILLs
        # the container at the same budget — belt and braces).
        deadline = flags.wall_clock_seconds + 10
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._connect_timeout, read=float(deadline)),
                transport=self._transport,
            ) as client:
                started = await client.post("/runs", json=payload)
                started.raise_for_status()
                run_id = str(started.json()["id"])
                return await self._poll(client, run_id, deadline=deadline)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise DependencyError(
                "sandbox runner unavailable", code="sandbox_runner_unavailable"
            ) from exc

    async def _poll(
        self, client: httpx.AsyncClient, run_id: str, *, deadline: float
    ) -> RunResult:
        loop = asyncio.get_running_loop()
        end = loop.time() + deadline
        while True:
            resp = await client.get(f"/runs/{run_id}")
            resp.raise_for_status()
            body = resp.json()
            status = _map_status(body.get("status"))
            if status not in (CodeRunStatus.QUEUED, CodeRunStatus.RUNNING):
                return self._from_wire(body, status)
            if loop.time() >= end:
                # The runner did not terminate within budget — treat as timeout.
                return RunResult(
                    status=CodeRunStatus.TIMEOUT,
                    exit_code=None,
                    stdout=str(body.get("stdout", "")),
                    stderr=str(body.get("stderr", "")),
                    duration_ms=int(deadline * 1000),
                    image_digest=body.get("image_digest"),
                )
            await asyncio.sleep(self._poll_interval)

    @staticmethod
    def _to_wire(spec: RunSpec, flags: ContainerFlags) -> dict[str, object]:
        """The ``POST /runs`` body: the code + staged inputs + the enforced flags.

        The runner receives ONLY this — no host env is forwarded (G5): the sole env
        is the run's explicit curated ``env``. The enforced flags travel as data so
        the runner applies exactly the hardening this module decided.
        """
        return {
            "code": spec.code,
            "env": dict(spec.env),
            "inputs": [
                {
                    "ref_id": str(i.ref_id),
                    "dest_path": i.dest_path,
                    "data_b64": _b64(i.data),
                    "read_only": i.read_only,
                }
                for i in spec.inputs
            ],
            "container": {
                "network_mode": flags.network_mode,
                "read_only_rootfs": flags.read_only_rootfs,
                "tmpfs_scratch_bytes": flags.tmpfs_scratch_bytes,
                "run_as_non_root": flags.run_as_non_root,
                "no_new_privileges": flags.no_new_privileges,
                "cap_drop": list(flags.cap_drop),
                "seccomp_profile": flags.seccomp_profile,
                "cpus": flags.cpus,
                "memory_bytes": flags.memory_bytes,
                "pids_limit": flags.pids_limit,
                "wall_clock_seconds": flags.wall_clock_seconds,
                "output_bytes_cap": flags.output_bytes_cap,
                "runtime": flags.runtime,
                "egress_allowlist": list(flags.egress_allowlist),
            },
        }

    @staticmethod
    def _from_wire(body: dict[str, object], status: CodeRunStatus) -> RunResult:
        exit_code = body.get("exit_code")
        files = body.get("output_files")
        output_files: tuple[OutputFile, ...] = ()
        if isinstance(files, list):
            output_files = tuple(
                OutputFile(
                    filename=str(f["filename"]),
                    content_type=str(f.get("content_type", "application/octet-stream")),
                    data=_unb64(str(f["data_b64"])),
                )
                for f in files
                if isinstance(f, dict) and "filename" in f and "data_b64" in f
            )
        raw_duration = body.get("duration_ms")
        duration_ms = raw_duration if isinstance(raw_duration, int) else 0
        raw_digest = body.get("image_digest")
        image_digest = raw_digest if isinstance(raw_digest, str) else None
        return RunResult(
            status=status,
            exit_code=exit_code if isinstance(exit_code, int) else None,
            stdout=str(body.get("stdout", "")),
            stderr=str(body.get("stderr", "")),
            duration_ms=duration_ms,
            output_files=output_files,
            image_digest=image_digest,
            resource_usage=ResourceUsage.from_dict(body.get("resource_usage")),
        )


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    import base64

    return base64.b64decode(text.encode("ascii"))
