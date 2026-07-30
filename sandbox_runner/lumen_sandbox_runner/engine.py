"""Docker implementation of reusable offline root containers (ADR-0020)."""

from __future__ import annotations

import base64
import contextlib
import io
import mimetypes
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from packaging.requirements import InvalidRequirement, Requirement

from lumen_sandbox_runner.models import ContainerPolicy, EnsureSessionRequest, ExecuteRequest

_MANAGED_LABEL = "com.lumen.sandbox.managed"
_SESSION_LABEL = "com.lumen.sandbox.session"
_GENERATION_LABEL = "com.lumen.sandbox.generation"
_IDLE_COMMAND = ["sh", "-lc", "while :; do sleep 3600; done"]

#: The OCI runtimes this runner will launch: the hardened Docker baseline, or gVisor.
_KNOWN_RUNTIMES = ("runc", "runsc")

#: Hard ceiling on how many bytes of ONE execution's output directory this process
#: will read into memory, whatever a caller asks for. ``get_archive`` streams a tar of
#: a MODEL-CONTROLLED directory: without a budget, one ``open('big','wb').write(...)``
#: in a chat turn takes the only Docker-socket holder past its container memory limit,
#: and the OOM killer removes code execution for every tenant. A session may request
#: LESS (the backend sends ``SANDBOX_OUTPUT_BYTES_CAP``); nothing may request more.
_OUTPUT_BYTES_CEILING = 64 * 1024 * 1024
#: Hard ceiling on collected output FILES per execution. The byte budget alone does
#: not bound this: a tar member costs ~1 KiB on the wire, so 32 MiB admits ~32k
#: members, and each one becomes an object-store PUT + an artifacts row + an audit
#: event in the caller's transaction. Generous for a chat turn, fatal as a fan-out.
_MAX_OUTPUT_MEMBERS = 64

#: Cap on the stdout/stderr this process RETAINS and returns per execution.
#: NOTE the honest limit: docker-py's non-streaming ``exec_run`` buffers the whole
#: process output before returning, so this bounds the JSON payload and the result
#: cache but NOT peak memory during the exec itself — ``print('x'*2_000_000_000)``
#: is still a runner-side OOM risk. Bounding that needs the streaming low-level exec
#: API, tracked separately; ADR-0013 §G7 and the runbook say so plainly rather than
#: claiming a bound that does not exist.
_MAX_STREAM_CHARS = 256 * 1024

#: How many completed execution results are retained for idempotent re-reads. The
#: cache is keyed per (session, generation, execution) and was previously unbounded,
#: so long-lived sessions accumulated every result — up to the output cap each —
#: until close. Oldest-first eviction keeps it bounded; an evicted key simply
#: re-executes rather than returning a stale answer.
_MAX_CACHED_RESULTS = 64

#: The effective budget rides on the container, so an execution against an
#: already-ensured generation enforces the number that generation was created with.
_OUTPUT_CAP_LABEL = "com.lumen.sandbox.output-cap"

#: A PEP-503 distribution name and nothing else. Asserted on the parsed name of every
#: requirement before it reaches a pip argv. It is defence in depth, deliberately: a
#: leading dash cannot survive ``Requirement()`` (PEP-508 requires a letter or digit
#: first), and the backend's admission layer parses the same string before this one
#: ever sees it. Both of those reject only what they are handed; this asserts the
#: property the argv actually depends on, so a future ``packaging`` relaxation cannot
#: quietly turn a "package name" into a pip option.
_DISTRIBUTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

#: The end-of-options separator. Everything after it is a positional argument, so pip
#: reads it as a requirement even if it is spelled like a flag (verified against the
#: shipped pip: ``pip download -- --index-url=…`` reports an *invalid requirement*,
#: while the same argv without the separator consumes it as an option).
_END_OF_OPTIONS = "--"


class RunnerError(RuntimeError):
    """Safe internal-runner failure carrying an HTTP status."""

    def __init__(self, message: str, *, status_code: int = 500) -> None:
        self.status_code = status_code
        super().__init__(message)


class _BudgetedReader:
    """A read-only file view over a chunk stream that stops at a hard byte budget.

    ``tarfile`` in stream mode (``r|*``) only ever calls ``read``, so this is the whole
    interface it needs. Once the budget is spent the reader reports EOF instead of
    pulling more of a model-controlled archive into memory; ``exhausted`` records that
    data was actually dropped (a stream that ends exactly on the budget is not
    truncated). Peak memory is the budget plus at most one source chunk.
    """

    def __init__(self, chunks: Iterable[bytes], budget: int) -> None:
        self._chunks = iter(chunks)
        self._buffer = bytearray()
        self._remaining = max(budget, 0)
        self.exhausted = False

    def read(self, size: int = -1) -> bytes:
        # Once the budget is spent, stop pulling from the source stream altogether:
        # draining the rest of a huge archive costs nothing in memory but keeps the
        # daemon streaming it. Buffered bytes are still served, then EOF.
        while not self.exhausted and (size < 0 or len(self._buffer) < size):
            try:
                chunk = next(self._chunks)
            except StopIteration:
                break
            if not chunk:
                continue
            if len(chunk) > self._remaining:
                # Keep only what the budget still allows, then stop reading entirely.
                self.exhausted = True
                if self._remaining > 0:
                    self._buffer.extend(chunk[: self._remaining])
                    self._remaining = 0
                break
            self._remaining -= len(chunk)
            self._buffer.extend(chunk)
        if size < 0:
            size = len(self._buffer)
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data


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
    def _clip(text: str) -> str:
        """Bound one stream's retained text, saying so in-band when it is cut."""
        if len(text) <= _MAX_STREAM_CHARS:
            return text
        return text[:_MAX_STREAM_CHARS] + (
            f"\n[output truncated at {_MAX_STREAM_CHARS} characters]"
        )

    def _remember(self, key: tuple[UUID, int, UUID], result: dict[str, object]) -> None:
        """Cache a completed result, evicting oldest-first past the bound."""
        self._results[key] = result
        while len(self._results) > _MAX_CACHED_RESULTS:
            self._results.pop(next(iter(self._results)), None)

    @staticmethod
    def _container_runtime(container: Any) -> str | None:
        """The runtime a container is ACTUALLY running under, or ``None`` if unreadable.

        Read from ``HostConfig.Runtime`` on the live inspect payload (``reload()`` has
        just refreshed it). ``None`` means the daemon did not report one — treated as
        "cannot prove a mismatch", so an unreadable value never destroys a working
        session; the launch-time checks still govern anything newly created.
        """
        attrs = getattr(container, "attrs", None)
        if not isinstance(attrs, dict):
            return None
        host_config = attrs.get("HostConfig")
        if not isinstance(host_config, dict):
            return None
        runtime = host_config.get("Runtime")
        return runtime if isinstance(runtime, str) and runtime else None

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
                self._remember(key, result)
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
                self._remember(key, result)
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
                # A reused container was created under whatever runtime was configured
                # THEN. If this service has since been repinned (runc → runsc), keeping
                # it would go on executing model code under the weaker runtime while the
                # backend — and the runbook — believe gVisor is enforced. That silent
                # downgrade is exactly the window a half-restarted deploy opens, so the
                # ACTUAL runtime is inspected, not assumed from creation time. A mismatch
                # is handled like any other "not the container we want": remove it and
                # create a fresh one below. Recreating (rather than refusing) keeps the
                # session available — ADR-0020 §1 makes the environment a cache, not
                # durable storage — while guaranteeing nothing runs under the wrong one.
                actual = self._container_runtime(container)
                if actual is not None and actual != self._runtime:
                    self._remove(container)
                    break
                if container.status != "running":
                    container.start()
                return container
            self._remove(container)

        # Preconditions of LAUNCH, checked before anything is created.
        self._require_agreed_runtime(request.container.runtime)
        launch_ref = self._require_local_image(request.image)
        labels = {
            _MANAGED_LABEL: "true",
            _SESSION_LABEL: str(session_id),
            _GENERATION_LABEL: str(request.generation),
            _OUTPUT_CAP_LABEL: str(self._effective_output_cap(request.container.output_bytes_cap)),
        }
        return self._client.containers.run(
            launch_ref,
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
            **self._resource_bounds(request.container),
        )

    @staticmethod
    def _resource_bounds(policy: ContainerPolicy) -> dict[str, object]:
        """Engine flags for the bounds a caller asked for — none, unless asked.

        ADR-0020's shipped posture is an unbounded execution container, so an absent
        value stays absent: this returns an empty mapping and ``containers.run`` is
        called exactly as before. A deploy that wants a bound gets the real engine flag
        rather than a field the schema accepted and nothing read, which is the state
        ``output_bytes_cap`` was found in.
        """
        bounds: dict[str, object] = {}
        if policy.memory_bytes is not None:
            bounds["mem_limit"] = policy.memory_bytes
        if policy.pids_limit is not None:
            bounds["pids_limit"] = policy.pids_limit
        if policy.cpus is not None:
            # Docker expresses a fractional CPU budget in billionths of a core.
            bounds["nano_cpus"] = int(policy.cpus * 1_000_000_000)
        return bounds

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
                    _END_OF_OPTIONS,
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
        # The engine's own path wins: it is what `_collect_outputs` reads from, so a
        # caller-supplied value could otherwise tell the model to write somewhere
        # collection never looks (silent zero-artifact success).
        env = {**request.env, "LUMEN_OUTPUT_DIR": output_dir}
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
            stdout = self._clip((stdout_raw or b"").decode("utf-8", errors="replace"))
            stderr = self._clip((stderr_raw or b"").decode("utf-8", errors="replace"))
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
        output_files: list[dict[str, object]] = []
        truncated = False
        if status != "killed":
            budget = self._output_cap_of(container)
            output_files, truncated = self._collect_outputs(container, output_dir, budget)
            if truncated:
                note = (
                    f"Only part of the output directory was collected: it exceeded the "
                    f"{budget}-byte limit for one execution. Write fewer or smaller files."
                )
                stderr = f"{stderr}\n{note}" if stderr else note
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

    def _require_agreed_runtime(self, requested: str) -> None:
        """Refuse a session whose caller believes in a different runtime than this one.

        The launched runtime is always ``self._runtime`` — a caller cannot select it.
        But silently overriding a disagreement hides the dangerous direction of it:
        both sides read the same ``SANDBOX_RUNTIME``, so they diverge only when an
        operator changed it and restarted the backend without recreating this service,
        which would leave a deploy that believes it is on gVisor launching containers
        under plain ``runc``. Refusing names both values instead.
        """
        if requested != self._runtime:
            raise RunnerError(
                f"sandbox runtime mismatch: this runner is configured for "
                f"'{self._runtime}' but the session asked for '{requested}'",
                status_code=409,
            )

    def _require_local_image(self, image: str) -> str:
        """Resolve an image the host daemon ALREADY has, and return its immutable id.

        docker-py's ``ContainerCollection.run`` catches ``ImageNotFound`` and pulls
        before retrying (docker 7.1.0, ``containers.py``). Relying on that, a typo'd or
        tampered ``SANDBOX_IMAGE`` turns into a registry fetch performed by the single
        process that holds the Docker socket — and the fetched image then executes as
        contained root. Resolving it first keeps image provenance an operator decision
        (build it, or ``docker pull`` the digest onto this host) and makes the failure
        an honest 503 instead of a silent download.

        A daemon that cannot answer at all lands here too: that is fail-closed by
        design — no session, no execution.

        Returning the resolved **id** closes the remaining TOCTOU window. Checking
        the reference here and then handing ``containers.run`` the same *reference*
        leaves a gap: if the image is pruned in between, docker-py's ImageNotFound
        arm pulls it after all. A local id (``sha256:…``) is not a pullable
        reference, so the same arm fails instead of fetching — and the container is
        launched from the exact bytes just verified, not from whatever the tag points
        at a moment later.
        """
        try:
            resolved = self._client.images.get(image)
        except Exception as exc:
            raise RunnerError(
                "sandbox execution image is not present on the host daemon", status_code=503
            ) from exc
        image_id = getattr(resolved, "id", None)
        return image_id if isinstance(image_id, str) and image_id else image

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
            if not _DISTRIBUTION_NAME.fullmatch(parsed.name):
                raise RunnerError("invalid package distribution name", status_code=422)
        return packages

    @staticmethod
    def _download_binary_packages(packages: tuple[str, ...], destination: Path) -> None:
        """Fetch binary wheels for admitted requirements.

        **This is the unverified half of the supply chain, and it is worth naming.**
        The fetch goes to the DEFAULT PUBLIC index with no ``--require-hashes``, no
        ``--index-url`` pin and no lockfile: ADR-0013 §3 conditions install-based
        expansion on "an admin-allowlisted, hash-pinned internal mirror", and that
        mirror is not implemented. An allow-list grant therefore trusts whatever PyPI
        serves at that moment. A locked-down deploy is expected to withhold this
        service's outbound network and live on the execution image's manifest.
        """
        command = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            os.fspath(destination),
            _END_OF_OPTIONS,
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
    def _effective_output_cap(requested: int | None) -> int:
        """The session's output budget: what was asked for, never above the ceiling."""
        if requested is None or requested > _OUTPUT_BYTES_CEILING:
            return _OUTPUT_BYTES_CEILING
        return max(requested, 1)

    @staticmethod
    def _output_cap_of(container: Any) -> int:
        """Read the budget the generation was created with; anything odd fails closed."""
        try:
            value = int(str((container.labels or {}).get(_OUTPUT_CAP_LABEL)))
        except (TypeError, ValueError):
            return _OUTPUT_BYTES_CEILING
        if value < 1:
            return _OUTPUT_BYTES_CEILING
        return min(value, _OUTPUT_BYTES_CEILING)

    @staticmethod
    def _collect_outputs(
        container: Any, output_dir: str, byte_budget: int
    ) -> tuple[list[dict[str, object]], bool]:
        """Collect the run's output files under a hard byte budget.

        The previous ``b"".join(stream)`` read a tar of a model-controlled directory
        into memory in full before anything looked at its size, so the size of one
        chat turn's output was the size of the runner's memory footprint. Streaming
        with a budget bounds it. A file the budget cuts in half is DROPPED rather than
        delivered short — a truncated PNG or xlsx persisted as an artifact would be a
        silently corrupt deliverable — and the caller is told collection was partial.
        """
        try:
            stream, _ = container.get_archive(output_dir)
        except Exception:
            # Report this as PARTIAL, not clean. Returning ``False`` here claimed a
            # successful empty collection, so a transient daemon error (or model code
            # that removed its own output dir) surfaced as "succeeded, no artifacts"
            # with nothing anywhere saying output was lost.
            return [], True
        reader = _BudgetedReader(stream, byte_budget)
        files: list[dict[str, object]] = []
        truncated = False
        try:
            with tarfile.open(fileobj=reader, mode="r|*") as tar:  # type: ignore[arg-type]
                for member in tar:
                    if not member.isfile():
                        continue
                    # A member costs only ~1 KiB on the wire (512-byte header + one
                    # data block), so a BYTE budget alone admits tens of thousands of
                    # them — and every one becomes an object-store PUT, an artifacts
                    # row and an audit event downstream. Bound the COUNT too.
                    if len(files) >= _MAX_OUTPUT_MEMBERS:
                        truncated = True
                        break
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        continue
                    data = extracted.read()
                    if len(data) != member.size:
                        truncated = True
                        break
                    filename = PurePosixPath(member.name).name
                    files.append(
                        {
                            "filename": filename,
                            "content_type": mimetypes.guess_type(filename)[0]
                            or "application/octet-stream",
                            "data_b64": base64.b64encode(data).decode("ascii"),
                        }
                    )
        except (tarfile.TarError, EOFError, OSError):
            # A tar cut short by the budget raises where the next header should be;
            # the members already read whole are still good.
            truncated = True
        finally:
            # ALWAYS release the docker stream. On the truncation path we stop pulling
            # from it, and docker-py deliberately disables the socket read timeout on
            # `get_archive`, so an abandoned generator leaves the daemon's tar writer
            # blocked on a socket nobody reads and the pooled connection never
            # reclaimed — until GC happens to collect it. The previous
            # ``b"".join(stream)`` always drained, so this leak arrived WITH the byte
            # budget; closing here is what makes the budget safe rather than a trade.
            close = getattr(stream, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
        return files, truncated or reader.exhausted


__all__ = ["DockerSandboxEngine", "RunnerError", "SessionLocks"]
