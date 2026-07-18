"""Reusable sandbox runner boundary and HTTP client (ADR-0020, issue #457)."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx

from app.core.errors import DependencyError
from app.domain.entities import CodeRunStatus, ResourceUsage
from app.sandbox.spec import OutputFile, RunResult, RunSpec, SandboxSessionSpec


@dataclass(frozen=True, slots=True)
class ContainerFlags:
    """Concrete isolation flags for a long-lived root-capable session container."""

    network_mode: str = "none"
    binds: tuple[str, ...] = ()
    read_only_rootfs: bool = False
    user: str = "0:0"
    cap_drop: tuple[str, ...] = ("ALL",)
    security_opt: tuple[str, ...] = ("no-new-privileges:true",)
    runtime: str = "runc"
    # ADR-0020 intentionally starts without automatic resource/time/output limits.
    cpus: float | None = None
    memory_bytes: int | None = None
    pids_limit: int | None = None
    wall_clock_seconds: int | None = None
    output_bytes_cap: int | None = None


def build_container_flags(spec: SandboxSessionSpec) -> ContainerFlags:
    """Return the fixed contained-root posture for a reusable session."""
    return ContainerFlags(runtime=spec.runtime)


class SandboxRunner(Protocol):
    """Domain-only boundary implemented by the dedicated runner HTTP service."""

    async def ensure_session(self, session: SandboxSessionSpec) -> None: ...

    async def execute(self, session: SandboxSessionSpec, run: RunSpec) -> RunResult: ...

    async def reset_session(
        self, previous: SandboxSessionSpec, replacement: SandboxSessionSpec
    ) -> None: ...

    async def close_session(self, session: SandboxSessionSpec) -> None: ...

    async def cancel(self, session: SandboxSessionSpec, execution_id: UUID) -> None: ...


def _map_status(raw: object) -> CodeRunStatus:
    """Unknown/malformed runner status fails closed."""
    try:
        return CodeRunStatus(str(raw))
    except ValueError:
        return CodeRunStatus.FAILED


class HttpSandboxRunner:
    """No-read-timeout client for the internal reusable-session runner API."""

    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._connect_timeout = connect_timeout_seconds
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        # Execution is deliberately unbounded. A finite connect/pool timeout catches
        # a missing runner, while `read=None` permits a run to finish or be cancelled.
        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=None,
            write=60.0,
            pool=self._connect_timeout,
        )
        return httpx.AsyncClient(
            base_url=self._base_url, timeout=timeout, transport=self._transport
        )

    async def ensure_session(self, session: SandboxSessionSpec) -> None:
        await self._request(
            "PUT", f"/sessions/{session.sandbox_session_id}", json=self._session_wire(session)
        )

    async def execute(self, session: SandboxSessionSpec, run: RunSpec) -> RunResult:
        await self.ensure_session(session)
        response = await self._request(
            "POST",
            f"/sessions/{session.sandbox_session_id}/executions",
            json={
                "generation": session.generation,
                "execution_id": str(run.execution_id),
                "code": run.code,
                "packages": list(run.packages),
                "env": dict(run.env),
                # Packages are acquired/installed by the runner before these tenant
                # bytes are staged, so package resolution never sees tenant data.
                "inputs": [
                    {
                        "ref_id": str(value.ref_id),
                        "dest_path": value.dest_path,
                        "data_b64": _b64(value.data),
                        "read_only": value.read_only,
                    }
                    for value in run.inputs
                ],
            },
        )
        body = response.json()
        if not isinstance(body, dict):
            raise DependencyError(
                "sandbox runner returned an invalid result",
                code="sandbox_runner_invalid_response",
            )
        return self._from_wire(body, _map_status(body.get("status")))

    async def reset_session(
        self, previous: SandboxSessionSpec, replacement: SandboxSessionSpec
    ) -> None:
        await self.close_session(previous)
        await self.ensure_session(replacement)

    async def close_session(self, session: SandboxSessionSpec) -> None:
        await self._request(
            "DELETE",
            f"/sessions/{session.sandbox_session_id}",
            params={"generation": session.generation},
        )

    async def cancel(self, session: SandboxSessionSpec, execution_id: UUID) -> None:
        await self._request(
            "POST",
            f"/sessions/{session.sandbox_session_id}/cancel",
            json={"generation": session.generation, "execution_id": str(execution_id)},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
        params: Mapping[str, str | int] | None = None,
    ) -> httpx.Response:
        try:
            async with self._client() as client:
                response = await client.request(method, path, json=json, params=params)
                response.raise_for_status()
                return response
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise DependencyError(
                "sandbox runner unavailable", code="sandbox_runner_unavailable"
            ) from exc

    @staticmethod
    def _session_wire(session: SandboxSessionSpec) -> dict[str, object]:
        flags = build_container_flags(session)
        return {
            "generation": session.generation,
            "image": session.image,
            "env": dict(session.env),
            "container": {
                "network_mode": flags.network_mode,
                "binds": list(flags.binds),
                "read_only_rootfs": flags.read_only_rootfs,
                "user": flags.user,
                "cap_drop": list(flags.cap_drop),
                "security_opt": list(flags.security_opt),
                "runtime": flags.runtime,
                "cpus": flags.cpus,
                "memory_bytes": flags.memory_bytes,
                "pids_limit": flags.pids_limit,
                "wall_clock_seconds": flags.wall_clock_seconds,
                "output_bytes_cap": flags.output_bytes_cap,
            },
        }

    @staticmethod
    def _from_wire(body: dict[str, object], status: CodeRunStatus) -> RunResult:
        raw_files = body.get("output_files")
        output_files: tuple[OutputFile, ...] = ()
        if isinstance(raw_files, list):
            output_files = tuple(
                OutputFile(
                    filename=str(value["filename"]),
                    content_type=str(
                        value.get("content_type", "application/octet-stream")
                    ),
                    data=_unb64(str(value["data_b64"])),
                )
                for value in raw_files
                if isinstance(value, dict)
                and "filename" in value
                and "data_b64" in value
            )
        raw_exit = body.get("exit_code")
        raw_duration = body.get("duration_ms")
        raw_digest = body.get("image_digest")
        return RunResult(
            status=status,
            exit_code=raw_exit if isinstance(raw_exit, int) else None,
            stdout=str(body.get("stdout", "")),
            stderr=str(body.get("stderr", "")),
            duration_ms=raw_duration if isinstance(raw_duration, int) else 0,
            output_files=output_files,
            image_digest=raw_digest if isinstance(raw_digest, str) else None,
            resource_usage=ResourceUsage.from_dict(body.get("resource_usage")),
        )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


__all__ = [
    "ContainerFlags",
    "HttpSandboxRunner",
    "SandboxRunner",
    "build_container_flags",
]
