"""Docker implementation of reusable offline root containers (ADR-0020)."""

from __future__ import annotations

import base64
import io
import mimetypes
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from packaging.requirements import InvalidRequirement, Requirement

from lumen_sandbox_runner.models import EnsureSessionRequest, ExecuteRequest

_MANAGED_LABEL = "com.lumen.sandbox.managed"
_SESSION_LABEL = "com.lumen.sandbox.session"
_GENERATION_LABEL = "com.lumen.sandbox.generation"
_IDLE_COMMAND = ["sh", "-lc", "while :; do sleep 3600; done"]

#: The OCI runtimes this runner will launch: the hardened Docker baseline, or gVisor.
_KNOWN_RUNTIMES = ("runc", "runsc")


class RunnerError(RuntimeError):
    """Safe internal-runner failure carrying an HTTP status."""

    def __init__(self, message: str, *, status_code: int = 500) -> None:
        self.status_code = status_code
        super().__init__(message)


class SessionLocks:
    """Stable per-session mutexes; executions in one environment never overlap."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[UUID, threading.RLock] = {}

    def get(self, session_id: UUID) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(session_id, threading.RLock())


class DockerSandboxEngine:
    """The only code that holds and uses the Docker socket."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        package_downloader: Callable[[tuple[str, ...], Path], None] | None = None,
        runtime: str | None = None,
    ) -> None:
        self._client = client if client is not None else self._docker_client()
        self._locks = SessionLocks()
        self._download = package_downloader or self._download_binary_packages
        self._results: dict[tuple[UUID, int, UUID], dict[str, object]] = {}
        self._runtime = self._configured_runtime(runtime)

    @staticmethod
    def _docker_client() -> Any:
        import docker

        return docker.from_env()

    @staticmethod
    def _configured_runtime(explicit: str | None) -> str:
        """Resolve the OCI runtime from THIS SERVICE's configuration, never a request.

        ``EnsureSessionRequest`` still carries ``runtime`` (the backend sends its own
        ``SANDBOX_RUNTIME`` and older payloads must keep validating), but a
        caller-selectable runtime means a deploy whose entire safety argument rests on
        gVisor can be downgraded to ``runc`` for one session by whoever can reach the
        internal API. The sandbox host decides; a value it does not recognise fails
        closed rather than degrading to the Docker baseline.
        """
        value = explicit if explicit is not None else os.environ.get("SANDBOX_RUNTIME", "runc")
        value = value.strip()
        if value not in _KNOWN_RUNTIMES:
            raise RunnerError(
                "sandbox runner SANDBOX_RUNTIME must be 'runc' or 'runsc'", status_code=500
            )
        return value

    def ensure(self, session_id: UUID, request: EnsureSessionRequest) -> dict[str, object]:
        with self._locks.get(session_id):
            container = self._ensure_unlocked(session_id, request)
            return self._session_result(container, session_id, request.generation)

    def inspect(self, session_id: UUID) -> dict[str, object]:
        containers = self._containers(session_id)
        if not containers:
            raise RunnerError("sandbox session not found", status_code=404)
        container = containers[0]
        generation = int(container.labels[_GENERATION_LABEL])
        return self._session_result(container, session_id, generation)

    def execute(
        self,
        session_id: UUID,
        session_request: EnsureSessionRequest,
        request: ExecuteRequest,
    ) -> dict[str, object]:
        key = (session_id, request.generation, request.execution_id)
        with self._locks.get(session_id):
            cached = self._results.get(key)
            if cached is not None:
                return cached
            container = self._ensure_unlocked(session_id, session_request)
            result = self._execute_unlocked(container, request)
            if result.get("status") != "killed":
                self._results[key] = result
            return result

    def execute_existing(self, session_id: UUID, request: ExecuteRequest) -> dict[str, object]:
        """Execute only in an already-ensured matching generation."""
        key = (session_id, request.generation, request.execution_id)
        with self._locks.get(session_id):
            cached = self._results.get(key)
            if cached is not None:
                return cached
            matches = [
                value
                for value in self._containers(session_id)
                if int(value.labels[_GENERATION_LABEL]) == request.generation
            ]
            if not matches:
                raise RunnerError(
                    "sandbox session must be ensured before execution", status_code=409
                )
            container = matches[0]
            result = self._execute_unlocked(container, request)
            if result.get("status") != "killed":
                self._results[key] = result
            return result

    def close(self, session_id: UUID, generation: int | None = None) -> None:
        # Do not wait on the execution lock: close/cancel is the recovery path for
        # an unbounded process. Removing the container terminates its active exec.
        for container in self._containers(session_id):
            current = int(container.labels[_GENERATION_LABEL])
            if generation is None or current == generation:
                self._remove(container)
        for key in tuple(self._results):
            if key[0] == session_id and (generation is None or key[1] == generation):
                self._results.pop(key, None)

    def cancel(self, session_id: UUID, generation: int, execution_id: UUID) -> None:
        del execution_id  # container teardown is intentionally generation-wide
        self.close(session_id, generation)

    def _ensure_unlocked(self, session_id: UUID, request: EnsureSessionRequest) -> Any:
        for container in self._containers(session_id):
            current = int(container.labels[_GENERATION_LABEL])
            if current == request.generation:
                container.reload()
                if container.status != "running":
                    container.start()
                return container
            self._remove(container)

        labels = {
            _MANAGED_LABEL: "true",
            _SESSION_LABEL: str(session_id),
            _GENERATION_LABEL: str(request.generation),
        }
        self._require_local_image(request.image)
        return self._client.containers.run(
            request.image,
            command=_IDLE_COMMAND,
            detach=True,
            name=f"lumen-sandbox-{session_id}-{request.generation}",
            labels=labels,
            environment=request.env,
            network_mode="none",
            read_only=False,
            user="0:0",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            runtime=self._runtime,
            volumes=None,
            working_dir="/workspace",
            # A tiny PID 1 reaps orphaned descendants between executions in the
            # long-lived container; it does not impose a process/time limit.
            init=True,
        )

    def _execute_unlocked(self, container: Any, request: ExecuteRequest) -> dict[str, object]:
        run_root = f"/workspace/.lumen/runs/{request.execution_id}"
        output_dir = f"{run_root}/output"
        self._exec_checked(container, ["mkdir", "-p", output_dir, "/workspace/inputs"])

        if request.packages:
            packages = self._validated_packages(tuple(request.packages))
            with tempfile.TemporaryDirectory(prefix="lumen-wheels-") as temp:
                wheelhouse = Path(temp)
                self._download(packages, wheelhouse)
                self._put_directory(container, wheelhouse, f"{run_root}/wheelhouse")
            self._exec_checked(
                container,
                [
                    "python",
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--find-links",
                    f"{run_root}/wheelhouse",
                    *packages,
                ],
            )

        # Tenant bytes are staged only after package acquisition/installation.
        for value in request.inputs:
            target = self._safe_input_path(value.dest_path)
            self._put_bytes(container, target, base64.b64decode(value.data_b64, validate=True))
            self._exec_checked(container, ["chmod", "0444", target])

        code_path = f"{run_root}/main.py"
        self._put_bytes(container, code_path, request.code.encode("utf-8"))
        env = {"LUMEN_OUTPUT_DIR": output_dir, **request.env}
        started = time.monotonic()
        try:
            outcome = container.exec_run(
                ["python", code_path],
                workdir="/workspace",
                environment=env,
                demux=True,
            )
            exit_code = int(outcome.exit_code)
            stdout_raw, stderr_raw = outcome.output or (b"", b"")
            stdout = (stdout_raw or b"").decode("utf-8", errors="replace")
            stderr = (stderr_raw or b"").decode("utf-8", errors="replace")
            if exit_code != 0 and not self._container_running(container):
                # Docker returns exit 137 rather than raising when cancel removes a
                # container during exec. Treat any non-zero outcome from a container
                # that no longer exists/runs as killed; a user process exiting 137
                # while its container remains healthy is still a normal failure.
                exit_code = None
                stdout = ""
                stderr = "Code execution was cancelled or the sandbox container stopped."
                status = "killed"
            else:
                status = "succeeded" if exit_code == 0 else "failed"
        except Exception as exc:  # container removal during cancel is a killed run
            exit_code = None
            stdout = ""
            stderr = "Code execution was cancelled or the sandbox container stopped."
            status = "killed"
            if self._container_running(container):
                raise RunnerError("sandbox execution failed") from exc
        duration_ms = int((time.monotonic() - started) * 1000)
        output_files = self._collect_outputs(container, output_dir) if status != "killed" else []
        output_bytes = len(stdout.encode()) + len(stderr.encode())
        return {
            "status": status,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": duration_ms,
            "image_digest": self._image_digest(container),
            "output_files": output_files,
            "resource_usage": {"output_bytes": output_bytes},
        }

    def _require_local_image(self, image: str) -> None:
        """Launch only an image the host daemon ALREADY has — never trigger a pull.

        docker-py's ``ContainerCollection.run`` catches ``ImageNotFound`` and pulls
        before retrying (docker 7.1.0, ``containers.py``). Relying on that, a typo'd or
        tampered ``SANDBOX_IMAGE`` turns into a registry fetch performed by the single
        process that holds the Docker socket — and the fetched image then executes as
        contained root. Resolving it first keeps image provenance an operator decision
        (build it, or ``docker pull`` the digest onto this host) and makes the failure
        an honest 503 instead of a silent download.

        A daemon that cannot answer at all lands here too: that is fail-closed by
        design — no session, no execution.
        """
        try:
            self._client.images.get(image)
        except Exception as exc:
            raise RunnerError(
                "sandbox execution image is not present on the host daemon", status_code=503
            ) from exc

    def _containers(self, session_id: UUID) -> list[Any]:
        return list(
            self._client.containers.list(
                all=True,
                filters={"label": [f"{_MANAGED_LABEL}=true", f"{_SESSION_LABEL}={session_id}"]},
            )
        )

    @staticmethod
    def _remove(container: Any) -> None:
        try:
            container.remove(force=True)
        except Exception as exc:
            raise RunnerError("could not destroy sandbox container") from exc

    @staticmethod
    def _container_running(container: Any) -> bool:
        try:
            container.reload()
            return container.status == "running"
        except Exception:
            return False

    @staticmethod
    def _session_result(container: Any, session_id: UUID, generation: int) -> dict[str, object]:
        return {
            "sandbox_session_id": str(session_id),
            "generation": generation,
            "status": "active",
            "image_digest": DockerSandboxEngine._image_digest(container),
        }

    @staticmethod
    def _image_digest(container: Any) -> str:
        try:
            return str(container.image.attrs.get("RepoDigests", [container.image.id])[0])
        except Exception:
            return "unknown"

    @staticmethod
    def _validated_packages(packages: tuple[str, ...]) -> tuple[str, ...]:
        for raw in packages:
            try:
                parsed = Requirement(raw)
            except InvalidRequirement as exc:
                raise RunnerError("invalid package requirement", status_code=422) from exc
            if parsed.url is not None:
                raise RunnerError("direct URL packages are not permitted", status_code=422)
        return packages

    @staticmethod
    def _download_binary_packages(packages: tuple[str, ...], destination: Path) -> None:
        command = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            os.fspath(destination),
            *packages,
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)  # noqa: S603
        except subprocess.CalledProcessError as exc:
            raise RunnerError(
                "an approved package could not be downloaded", status_code=422
            ) from exc

    @staticmethod
    def _safe_input_path(raw: str) -> str:
        path = PurePosixPath(raw)
        parts = tuple(part for part in path.parts if part not in ("", "/", "."))
        if not parts or ".." in parts:
            raise RunnerError("invalid staged-input path", status_code=422)
        return str(PurePosixPath("/workspace/inputs", *parts))

    @staticmethod
    def _put_bytes(container: Any, target: str, data: bytes) -> None:
        path = PurePosixPath(target)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            info = tarfile.TarInfo(path.name)
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
        DockerSandboxEngine._exec_checked(container, ["mkdir", "-p", str(path.parent)])
        if not container.put_archive(str(path.parent), archive.getvalue()):
            raise RunnerError("could not stage sandbox bytes")

    @staticmethod
    def _put_directory(container: Any, source: Path, target: str) -> None:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for path in sorted(source.iterdir()):
                tar.add(path, arcname=path.name, recursive=False)
        DockerSandboxEngine._exec_checked(container, ["mkdir", "-p", target])
        if not container.put_archive(target, archive.getvalue()):
            raise RunnerError("could not stage package wheels")

    @staticmethod
    def _exec_checked(container: Any, command: list[str]) -> None:
        outcome = container.exec_run(command, demux=True)
        if int(outcome.exit_code) != 0:
            raise RunnerError("sandbox preparation command failed", status_code=422)

    @staticmethod
    def _collect_outputs(container: Any, output_dir: str) -> list[dict[str, object]]:
        try:
            stream, _ = container.get_archive(output_dir)
        except Exception:
            return []
        raw = b"".join(stream)
        files: list[dict[str, object]] = []
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                filename = PurePosixPath(member.name).name
                files.append(
                    {
                        "filename": filename,
                        "content_type": mimetypes.guess_type(filename)[0]
                        or "application/octet-stream",
                        "data_b64": base64.b64encode(extracted.read()).decode("ascii"),
                    }
                )
        return files


__all__ = ["DockerSandboxEngine", "RunnerError", "SessionLocks"]
