from __future__ import annotations

import base64
import builtins
import io
import tarfile
import threading
import time
import tracemalloc
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from lumen_sandbox_runner.engine import (
    _DISTRIBUTION_NAME,
    _MAX_STREAM_CHARS,
    _OUTPUT_BYTES_CEILING,
    _OUTPUT_CAP_LABEL,
    DockerSandboxEngine,
    RunnerError,
)
from lumen_sandbox_runner.models import ContainerPolicy, EnsureSessionRequest, ExecuteRequest


class _Image:
    id = "sha256:image"
    tags = ["lumen-sandbox-runner:0.2.0"]
    attrs = {"RepoDigests": ["lumen-sandbox-runner@sha256:abc"]}


class _Container:
    def __init__(self, labels: dict[str, str], events: list[str]) -> None:
        self.labels = labels
        self.events = events
        #: Model-code execs now go through the low-level streaming API (#519), which
        #: addresses containers by id rather than by object.
        self.id = f"container-{id(self):x}"
        #: How the streamed stdout is delivered. A test can make this many small
        #: chunks to prove the engine never holds the whole output at once.
        self.stream_chunks: list[bytes] | None = None
        self.image = _Image()
        self.attrs = {"Config": {"Env": []}, "HostConfig": {"Runtime": "runc"}}
        self.status = "running"
        self.removed = False
        self.python_calls = 0
        self._active = 0
        self.max_active = 0
        self.execution_exit_code = 0
        self.stop_on_execute = False
        self._guard = threading.Lock()
        # What the model wrote to the output dir: (filename, byte length). The
        # archive is handed back in small chunks so a test can observe how much of
        # a model-controlled stream the engine was willing to read.
        self.output_files: list[tuple[str, int]] = []
        # Every argv the engine ran inside the container, so a test can assert on the
        # exact command line rather than only on the order things happened in.
        self.commands: list[list[str]] = []
        self.archive_chunk_size = 256
        self.archive_chunks_yielded = 0
        #: Set when the archive generator is CLOSED explicitly (GeneratorExit),
        #: i.e. the consumer released it instead of abandoning it mid-stream.
        self.archive_stream_closed = False
        #: A HELD reference to the generator. Without this the test cannot tell an
        #: explicit ``close()`` from CPython reclaiming the abandoned generator the
        #: instant ``_collect_outputs`` returns — refcounting fires GeneratorExit
        #: either way, which made the first version of that assertion vacuous.
        #: Holding it here reproduces the real hazard: docker-py's response is kept
        #: alive by the connection pool, so only a deliberate close releases it.
        self.archive_stream: Iterator[bytes] | None = None
        self.archive_chunks_total = 0

    def reload(self) -> None:
        return None

    def start(self) -> None:
        self.status = "running"

    def remove(self, *, force: bool) -> None:
        assert force is True
        self.removed = True
        self.status = "exited"

    def exec_run(self, command: list[str], **kwargs: object) -> object:
        del kwargs
        self.commands.append(list(command))
        if command[:4] == ["python", "-m", "pip", "install"]:
            self.events.append("pip-install")
        elif command and command[0] == "python":
            # Model code no longer arrives here — it goes through the engine's
            # streaming exec (#519). Anything still reaching this branch is setup.
            return self._run_model_code()
        return SimpleNamespace(exit_code=0, output=(b"", b""))

    def _run_model_code(self) -> Any:
        """The bookkeeping both exec paths share, so tests observe it either way."""
        self.events.append("python")
        self.python_calls += 1
        with self._guard:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        time.sleep(0.03)
        with self._guard:
            self._active -= 1
        if self.stop_on_execute:
            self.status = "exited"
        return SimpleNamespace(
            exit_code=self.execution_exit_code,
            output=(b"ok\n" if self.execution_exit_code == 0 else b"", b""),
        )

    def put_archive(self, path: str, data: bytes) -> bool:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            names = archive.getnames()
        if "wheelhouse" in path:
            self.events.append("wheelhouse")
        elif any("input" in name for name in names) or "/inputs" in path:
            self.events.append("input")
        elif any(name == "main.py" for name in names):
            self.events.append("code")
        return True

    def get_archive(self, path: str) -> tuple[Iterator[bytes], dict[str, object]]:
        del path
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            for name, size in self.output_files:
                info = tarfile.TarInfo(f"output/{name}")
                info.size = size
                archive.addfile(info, io.BytesIO(b"z" * size))
        raw = data.getvalue()
        chunks = [
            raw[start : start + self.archive_chunk_size]
            for start in range(0, len(raw), self.archive_chunk_size)
        ]
        self.archive_chunks_total = len(chunks)
        self.archive_chunks_yielded = 0

        def _stream() -> Iterator[bytes]:
            try:
                for chunk in chunks:
                    self.archive_chunks_yielded += 1
                    yield chunk
            except GeneratorExit:
                self.archive_stream_closed = True
                raise

        self.archive_stream = _stream()
        return self.archive_stream, {}


class _ImageNotFound(Exception):
    """Stands in for ``docker.errors.ImageNotFound``."""


class _Images:
    """The host daemon's local image store, plus the pull docker-py would perform."""

    def __init__(self) -> None:
        self.present = {"lumen-sandbox-runner:0.2.0", "lumen-sandbox-exec:0.1.1"}
        self.gets: list[str] = []
        self.pulls: list[str] = []

    def get(self, name: str) -> object:
        self.gets.append(name)
        if name not in self.present:
            raise _ImageNotFound(name)
        return _Image()

    def pull(self, name: str, **kwargs: object) -> object:  # pragma: no cover - must not run
        del kwargs
        self.pulls.append(name)
        return _Image()


class _Containers:
    def __init__(self) -> None:
        self.values: list[_Container] = []
        self.run_kwargs: list[dict[str, object]] = []

    def run(self, image: str, **kwargs: object) -> _Container:
        del image
        self.run_kwargs.append(kwargs)
        container = _Container(dict(kwargs["labels"]), [])  # type: ignore[arg-type]
        # Mirror the daemon: the container reports the runtime it was created with,
        # which is what the reuse path inspects rather than assuming.
        runtime = kwargs.get("runtime")
        if isinstance(runtime, str):
            container.attrs["HostConfig"]["Runtime"] = runtime
        self.values.append(container)
        return container

    def list(self, *, all: bool, filters: dict[str, list[str]]) -> list[_Container]:
        assert all is True
        expected = {
            item.split("=", 1)[0]: item.split("=", 1)[1] for item in filters.get("label", [])
        }
        return [
            value
            for value in self.values
            if not value.removed
            and builtins.all(
                value.labels.get(key) == expected_value for key, expected_value in expected.items()
            )
        ]


class _Api:
    """The low-level `client.api` the streaming model-code exec uses (#519).

    Deliberately narrow: only the three calls the engine makes. Setup commands still
    go through `container.exec_run`, so this seam stays small.
    """

    def __init__(self, containers: _Containers) -> None:
        self._containers = containers
        self._execs: dict[str, _Container] = {}
        self._exit_codes: dict[str, int] = {}

    def _find(self, container_id: str) -> _Container:
        for value in self._containers.values:
            if value.id == container_id:
                return value
        raise KeyError(container_id)

    def exec_create(self, container_id: str, command: list[str], **kwargs: object) -> dict:
        del kwargs
        container = self._find(container_id)
        container.commands.append(list(command))
        exec_id = f"exec-{len(self._execs)}"
        self._execs[exec_id] = container
        return {"Id": exec_id}

    def exec_start(self, exec_id: str, *, stream: bool = False, demux: bool = False) -> object:
        del stream, demux
        container = self._execs[exec_id]
        outcome = container._run_model_code()
        self._exit_codes[exec_id] = int(outcome.exit_code)
        if container.stream_chunks is not None:
            # LAZY on purpose. A list comprehension here would pull every chunk out
            # of the source the moment the stream is created, so a test counting how
            # much the ENGINE consumed would count the fake's own materialisation
            # instead — and would pass whether or not the engine drained anything.
            def _yield_chunks(source: Iterable[bytes]) -> Iterator[tuple[bytes, None]]:
                for chunk in source:
                    yield (chunk, None)

            return _yield_chunks(container.stream_chunks)
        stdout, stderr = outcome.output
        return iter([(stdout or None, stderr or None)])

    def exec_inspect(self, exec_id: str) -> dict:
        return {"ExitCode": self._exit_codes.get(exec_id, 0)}


class _Client:
    def __init__(self) -> None:
        self.containers = _Containers()
        self.images = _Images()
        self.api = _Api(self.containers)


def _ensure(
    *, generation: int = 1, output_bytes_cap: int | None = None, runtime: str = "runc"
) -> EnsureSessionRequest:
    return EnsureSessionRequest(
        generation=generation,
        image="lumen-sandbox-runner:0.2.0",
        env={"HOME": "/root"},
        container={"runtime": runtime, "output_bytes_cap": output_bytes_cap},
    )


def test_wire_policy_rejects_any_isolation_widening() -> None:
    with pytest.raises(ValidationError):
        ContainerPolicy(network_mode="bridge")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ContainerPolicy(binds=["/host:/workspace"])
    with pytest.raises(ValidationError):
        ContainerPolicy(read_only_rootfs=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ContainerPolicy(cap_drop=["ALL", "NET_ADMIN"])  # type: ignore[list-item]
    # No wall clock is enforced anywhere in this service, so the field stays ``None``:
    # accepting a number would be a promise no code keeps (ADR-0020 replaced
    # ADR-0013's automatic timeout with explicit cancel/reset).
    with pytest.raises(ValidationError):
        ContainerPolicy(wall_clock_seconds=30)  # type: ignore[arg-type]
    # ``output_bytes_cap`` is the one policy value a caller may set, because lowering
    # it only narrows what the runner will hold in memory. Zero would mean "collect
    # nothing" by accident rather than by decision, and negative is nonsense.
    with pytest.raises(ValidationError):
        ContainerPolicy(output_bytes_cap=0)
    with pytest.raises(ValidationError):
        EnsureSessionRequest(
            generation=1,
            image="python",
            env={"DATABASE_URL": "secret"},
        )


# --- The bounds ADR-0020 ships WITHOUT, but a deploy may now ask for (C5) ------
#
# ADR-0020 launches the execution container with no cpu, memory or PID limit. That is
# a sponsor decision and the design's largest residual operational risk — one
# `run_python` call can exhaust host RAM, saturate every core, or fork-bomb, and the
# only recovery is explicit cancel. Typing these fields `None` went further than the
# decision, though: it made the posture unchangeable by configuration, so a deployer
# who WANTED a bound could not even ask. They narrow only, so a caller may set them.


def test_no_resource_bound_is_applied_unless_one_is_asked_for() -> None:
    """The shipped posture is unchanged: absent values mean absent engine flags."""
    client = _Client()
    engine = DockerSandboxEngine(client)
    engine.ensure(uuid4(), _ensure())

    kwargs = client.containers.run_kwargs[0]
    assert "mem_limit" not in kwargs
    assert "pids_limit" not in kwargs
    assert "nano_cpus" not in kwargs


def test_a_requested_cpu_memory_and_pid_bound_reaches_the_engine() -> None:
    """A bound a deploy asks for must become a real flag, not an accepted-and-ignored
    field — which is exactly the state ``output_bytes_cap`` was found in."""
    client = _Client()
    engine = DockerSandboxEngine(client)
    engine.ensure(
        uuid4(),
        EnsureSessionRequest(
            generation=1,
            image="lumen-sandbox-runner:0.2.0",
            env={"HOME": "/root"},
            container={"memory_bytes": 512 * 1024 * 1024, "pids_limit": 128, "cpus": 1.5},
        ),
    )

    kwargs = client.containers.run_kwargs[0]
    assert kwargs["mem_limit"] == 512 * 1024 * 1024
    assert kwargs["pids_limit"] == 128
    assert kwargs["nano_cpus"] == 1_500_000_000
    # Nothing about containment moved to make room for them.
    assert kwargs["network_mode"] == "none"
    assert kwargs["cap_drop"] == ["ALL"]


def test_a_bound_the_engine_would_refuse_is_rejected_at_the_wire() -> None:
    """A non-positive bound is not "no bound" — it is a container Docker will not create.

    Docker's floor for ``--memory`` is 6 MiB. Refusing here makes the failure a
    readable 422 on one session instead of a container-creation error that looks like
    the runner is broken.
    """
    for hopeless in ({"memory_bytes": 1024}, {"memory_bytes": 0}, {"pids_limit": 0}, {"cpus": 0}):
        with pytest.raises(ValidationError):
            ContainerPolicy(**hopeless)  # type: ignore[arg-type]


def test_ensure_reuses_one_root_writable_offline_container() -> None:
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())
    engine.ensure(session_id, _ensure())

    assert len(client.containers.run_kwargs) == 1
    kwargs = client.containers.run_kwargs[0]
    assert kwargs["network_mode"] == "none"
    assert kwargs["read_only"] is False
    assert kwargs["user"] == "0:0"
    assert kwargs["volumes"] is None
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["init"] is True
    assert "mem_limit" not in kwargs
    assert "nano_cpus" not in kwargs
    assert "pids_limit" not in kwargs


def test_missing_execution_image_fails_closed_instead_of_pulling_it() -> None:
    """docker-py pulls on a local miss — the runner must resolve the image first.

    ``ContainerCollection.run`` catches ``ImageNotFound`` and pulls before retrying
    (docker 7.1.0, ``containers.py``). Left to it, a typo'd or tampered
    ``SANDBOX_IMAGE`` becomes a registry fetch performed by the one process holding
    the Docker socket, and the fetched image then executes as contained root. The
    runner launches only what the host daemon already has.
    """
    client = _Client()
    engine = DockerSandboxEngine(client)

    with pytest.raises(RunnerError) as refusal:
        engine.ensure(
            uuid4(),
            EnsureSessionRequest(generation=1, image="lumen-sandbox-exec:0.2.0-typo"),
        )

    assert refusal.value.status_code == 503
    assert "not present on the host daemon" in str(refusal.value)
    assert client.images.pulls == []
    assert client.containers.run_kwargs == []


def test_present_image_is_resolved_before_the_container_is_created() -> None:
    """The check is a precondition of launch, not a post-hoc audit."""
    client = _Client()
    engine = DockerSandboxEngine(client)
    engine.ensure(uuid4(), _ensure())

    assert client.images.gets == ["lumen-sandbox-runner:0.2.0"]
    assert len(client.containers.run_kwargs) == 1


def test_runtime_comes_from_the_environment_not_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SANDBOX_RUNTIME`` on the runner service is the single source of truth.

    A gVisor deploy's whole safety argument rests on the sandbox host's own
    configuration, so a caller cannot select the runtime: the launched value is read
    here (the constructor default is ``runc``, so a request for ``runsc`` only
    succeeds because the environment says so).
    """
    monkeypatch.setenv("SANDBOX_RUNTIME", "runsc")
    client = _Client()

    DockerSandboxEngine(client).ensure(uuid4(), _ensure(runtime="runsc"))

    assert client.containers.run_kwargs[0]["runtime"] == "runsc"


def test_a_runtime_the_caller_disagrees_with_is_refused_not_silently_applied() -> None:
    """A disagreement is a misconfiguration, and it must be loud.

    Both sides read the same ``SANDBOX_RUNTIME``, so they only diverge when an
    operator changed it and restarted one of them — most dangerously the backend but
    not this service, which would leave a deploy that believes it is on gVisor
    quietly launching containers under ``runc``. Refusing the session turns that
    silent downgrade into an error naming both values.
    """
    client = _Client()
    engine = DockerSandboxEngine(client, runtime="runsc")

    with pytest.raises(RunnerError) as refusal:
        engine.ensure(uuid4(), _ensure())  # the caller believes it is on runc

    assert "runsc" in str(refusal.value)
    assert client.containers.run_kwargs == []


def test_unknown_configured_runtime_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """A misconfigured runtime must not silently degrade to the Docker baseline."""
    monkeypatch.setenv("SANDBOX_RUNTIME", "firecracker")

    with pytest.raises(RunnerError):
        DockerSandboxEngine(_Client())


def test_packages_install_before_tenant_inputs_and_execution() -> None:
    client = _Client()

    def download(packages: tuple[str, ...], destination: Path) -> None:
        assert packages == ("numpy==2.1.0",)
        (destination / "numpy.whl").write_bytes(b"wheel")

    engine = DockerSandboxEngine(client, package_downloader=download)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())
    container = client.containers.values[0]
    request = ExecuteRequest(
        generation=1,
        execution_id=uuid4(),
        code="print('ok')",
        packages=["numpy==2.1.0"],
        inputs=[
            {
                "ref_id": str(uuid4()),
                "dest_path": "input.csv",
                "data_b64": "eA==",
                "read_only": True,
            }
        ],
    )
    engine.execute_existing(session_id, request)

    assert container.events.index("wheelhouse") < container.events.index("pip-install")
    assert container.events.index("pip-install") < container.events.index("input")
    assert container.events.index("input") < container.events.index("python")


# --- No requirement may reach pip as an OPTION (C4) ---------------------------
#
# Two layers already reject an option-shaped "package": the backend's admission path
# and this engine's own PEP-508 parse (a requirement must start with a letter or
# digit, so `--index-url=…` cannot parse). Both reject only what they are handed, and
# neither is a property of the argv. The argv now states the property itself: an
# end-of-options separator before the package list, so anything after it is a
# positional requirement even if it is spelled like a flag.


def test_an_option_shaped_requirement_is_refused_before_any_download() -> None:
    """The negative case that had no test: `--index-url=…` as a "package".

    A redirected index is the whole ballgame for a supply-chain attack — it decides
    which bytes get installed and executed as contained root. Refused with 422, and
    the downloader is never called, so nothing is fetched from anywhere.
    """
    client = _Client()
    downloads: list[tuple[str, ...]] = []

    def download(packages: tuple[str, ...], destination: Path) -> None:  # pragma: no cover
        del destination
        downloads.append(packages)

    engine = DockerSandboxEngine(client, package_downloader=download)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())

    for hostile in (
        "--index-url=http://evil.invalid/simple",
        "--extra-index-url=http://evil.invalid/simple",
        "-r/etc/passwd",
        "--trusted-host=evil.invalid",
    ):
        with pytest.raises(RunnerError) as refusal:
            engine.execute_existing(
                session_id,
                ExecuteRequest(
                    generation=1, execution_id=uuid4(), code="print(1)", packages=[hostile]
                ),
            )
        assert refusal.value.status_code == 422

    assert downloads == []


def test_the_wheelhouse_install_argv_ends_option_parsing_before_the_packages() -> None:
    """`pip install … -- <pkgs>`: the separator is in the argv, not just in the docs."""
    client = _Client()

    def download(packages: tuple[str, ...], destination: Path) -> None:
        del packages
        (destination / "numpy.whl").write_bytes(b"wheel")

    engine = DockerSandboxEngine(client, package_downloader=download)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())
    container = client.containers.values[0]
    engine.execute_existing(
        session_id,
        ExecuteRequest(
            generation=1, execution_id=uuid4(), code="print(1)", packages=["numpy==2.1.0"]
        ),
    )

    installs = [
        value for value in container.commands if value[:4] == ["python", "-m", "pip", "install"]
    ]
    assert installs, container.commands
    assert installs[0][-2:] == ["--", "numpy==2.1.0"]


def test_the_download_argv_ends_option_parsing_before_the_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same separator on the fetch side, which is the one that reaches the index."""
    captured: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> object:
        del kwargs
        captured.append(list(command))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("lumen_sandbox_runner.engine.subprocess.run", fake_run)

    DockerSandboxEngine._download_binary_packages(("numpy==2.1.0", "pandas"), Path("."))

    assert captured, "the downloader never ran"
    command = captured[0]
    assert command[-3:] == ["--", "numpy==2.1.0", "pandas"]
    assert "--only-binary=:all:" in command


def test_the_distribution_name_pattern_admits_real_names_and_nothing_option_shaped() -> None:
    """The asserted property: a parsed requirement's name is a name, never a flag."""
    assert all(
        _DISTRIBUTION_NAME.fullmatch(name)
        for name in ("numpy", "python-dateutil", "et_xmlfile", "zope.interface", "Pillow", "s3")
    )
    assert not any(
        _DISTRIBUTION_NAME.fullmatch(name)
        for name in ("--index-url", "-r", "-numpy", ".hidden", "numpy pandas", "")
    )


def test_same_session_executions_serialize_and_duplicate_is_cached() -> None:
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())
    first = ExecuteRequest(generation=1, execution_id=uuid4(), code="print(1)")
    second = ExecuteRequest(generation=1, execution_id=uuid4(), code="print(2)")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda request: engine.execute_existing(session_id, request),
                (first, second),
            )
        )
    container = client.containers.values[0]
    assert container.max_active == 1
    assert container.python_calls == 2

    engine.execute_existing(session_id, first)
    assert container.python_calls == 2


def _execute(engine: DockerSandboxEngine, session_id: object) -> dict[str, object]:
    return engine.execute_existing(
        session_id,  # type: ignore[arg-type]
        ExecuteRequest(generation=1, execution_id=uuid4(), code="print('ok')"),
    )


def test_collected_output_within_the_budget_is_returned_whole() -> None:
    """The cap is a bound on a hostile case, not a tax on the routine one."""
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure(output_bytes_cap=1_000_000))
    container = client.containers.values[0]
    container.output_files = [("chart.png", 900), ("table.csv", 400)]

    result = _execute(engine, session_id)

    files = result["output_files"]
    assert isinstance(files, list)
    assert [value["filename"] for value in files] == ["chart.png", "table.csv"]
    assert len(base64.b64decode(str(files[0]["data_b64"]))) == 900
    assert "exceeded" not in str(result["stderr"])
    assert container.archive_chunks_yielded == container.archive_chunks_total


def test_output_collection_stops_at_the_byte_budget_and_says_so() -> None:
    """One chat turn must not be able to OOM the only Docker-socket holder.

    ``get_archive`` streams a tar of a MODEL-CONTROLLED directory. Read whole into
    memory (``b"".join(stream)``) and then base64-encoded, a single
    ``open('big','wb').write(...)`` drives the runner past its container memory
    limit — and the OOM killer takes code execution away from every tenant, not just
    the one that ran it. So the collection is budgeted: complete files up to the cap
    are returned, the rest is dropped, and the run says what happened.
    """
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure(output_bytes_cap=2_000))
    container = client.containers.values[0]
    container.output_files = [(f"file{index}.bin", 800) for index in range(6)]

    result = _execute(engine, session_id)

    files = result["output_files"]
    assert isinstance(files, list)
    # The first member fits (512-byte header + 1024 bytes of padded data); the rest
    # of the stream is never pulled into the runner's memory at all.
    assert [value["filename"] for value in files] == ["file0.bin"]
    assert len(base64.b64decode(str(files[0]["data_b64"]))) == 800
    assert "exceeded" in str(result["stderr"])
    assert container.archive_chunks_yielded < container.archive_chunks_total
    assert container.archive_chunks_yielded * container.archive_chunk_size <= 2_000 + 256


def test_the_archive_stream_is_released_when_the_budget_stops_reading() -> None:
    """A budget that stops reading must also CLOSE the docker stream.

    This is the regression the byte budget itself introduced. The code it replaced
    (``b"".join(stream)``) always drained the archive to completion, so the response was
    always finished with. Streaming stops early — and docker-py deliberately DISABLES
    the socket read timeout on ``get_archive``, so an abandoned generator leaves the
    daemon's tar writer blocked on a socket nobody reads, with the pooled connection
    reclaimed only whenever GC happens to get to it. Every truncated collection would
    burn a daemon connection.
    """
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure(output_bytes_cap=2_000))
    container = client.containers.values[0]
    container.output_files = [(f"file{index}.bin", 800) for index in range(6)]

    result = _execute(engine, session_id)

    # Truncation really happened (otherwise this would pass vacuously on a short tar).
    assert container.archive_chunks_yielded < container.archive_chunks_total
    assert "exceeded" in str(result["stderr"])
    assert container.archive_stream_closed, (
        "the archive stream was abandoned rather than closed — the daemon-side writer "
        "stays blocked with no read timeout and the connection is never reclaimed"
    )


def test_a_reused_container_on_the_old_runtime_is_replaced_not_kept() -> None:
    """A half-restarted deploy must not keep executing under the weaker runtime.

    A container created while ``SANDBOX_RUNTIME=runc`` keeps running under runc. If this
    service is then repinned to ``runsc`` but an existing session is reused, the earlier
    code returned that container untouched — so model code went on running under the
    Docker baseline while the backend, the ADR and the runbook all believed gVisor was
    enforced. The container's ACTUAL runtime is now inspected, and a mismatch is treated
    like any other wrong container: removed and recreated.
    """
    client = _Client()
    old = DockerSandboxEngine(client, runtime="runc")
    session_id = uuid4()
    old.ensure(session_id, _ensure())
    first = client.containers.values[0]
    assert first.attrs["HostConfig"]["Runtime"] == "runc"

    # Same session, same generation — but this runner is pinned to gVisor.
    upgraded = DockerSandboxEngine(client, runtime="runsc")
    upgraded.ensure(session_id, _ensure(runtime="runsc"))

    assert first.removed, "the runc container was kept and would have executed model code"
    fresh = client.containers.values[-1]
    assert fresh is not first
    assert fresh.attrs["HostConfig"]["Runtime"] == "runsc"


def test_a_reused_container_on_the_current_runtime_is_kept() -> None:
    """The control: a matching runtime must NOT churn the session (state is a cache)."""
    client = _Client()
    engine = DockerSandboxEngine(client, runtime="runc")
    session_id = uuid4()
    engine.ensure(session_id, _ensure())
    first = client.containers.values[0]

    engine.ensure(session_id, _ensure())

    assert not first.removed
    assert len(client.containers.values) == 1


def test_output_collection_stops_at_the_member_ceiling() -> None:
    """Bytes alone do not bound members, and every member becomes a persisted artifact.

    A tar member costs ~1 KiB on the wire, so a byte budget admits tens of thousands of
    them — and each one downstream is an object-store PUT, an ``artifacts`` row and an
    audit event inside the caller's transaction. One chat turn writing 40k tiny files
    would fan out into 40k of each.
    """
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    # A generous byte budget, so ONLY the member ceiling can stop this.
    engine.ensure(session_id, _ensure(output_bytes_cap=8 * 1024 * 1024))
    container = client.containers.values[0]
    container.output_files = [(f"f{index}.txt", 1) for index in range(200)]

    result = _execute(engine, session_id)

    files = result["output_files"]
    assert isinstance(files, list)
    assert len(files) == 64
    assert "exceeded" in str(result["stderr"])


def test_a_failed_archive_read_is_reported_as_partial_not_clean() -> None:
    """A daemon error during collection must not read as "succeeded, no artifacts"."""
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())
    container = client.containers.values[0]
    container.output_files = [("chart.png", 128)]

    def _boom(path: str) -> tuple[Iterator[bytes], dict[str, object]]:
        del path
        raise RuntimeError("daemon said no")

    container.get_archive = _boom  # type: ignore[method-assign]

    result = _execute(engine, session_id)

    assert result["output_files"] == []
    # The run still succeeded, but the caller is TOLD output was lost.
    assert "exceeded" in str(result["stderr"]) or "truncat" in str(result["stderr"]).lower()


def test_a_partially_read_file_is_dropped_rather_than_delivered_corrupt() -> None:
    """Truncating in the middle of a file yields no file, not half a file."""
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure(output_bytes_cap=1_024))
    client.containers.values[0].output_files = [("report.xlsx", 4_096)]

    result = _execute(engine, session_id)

    assert result["output_files"] == []
    assert "exceeded" in str(result["stderr"])


def test_output_budget_is_clamped_to_the_runner_ceiling() -> None:
    """A caller may lower the budget; it can never raise it past the runner's own.

    The effective cap rides on the container as a label so an execution against an
    already-ensured generation (the production path) enforces the same number the
    session was created with.
    """
    client = _Client()
    engine = DockerSandboxEngine(client)
    engine.ensure(uuid4(), _ensure(output_bytes_cap=8 * 1024 * 1024 * 1024))
    engine.ensure(uuid4(), _ensure(output_bytes_cap=4_096))

    assert client.containers.values[0].labels[_OUTPUT_CAP_LABEL] == str(_OUTPUT_BYTES_CEILING)
    assert client.containers.values[1].labels[_OUTPUT_CAP_LABEL] == "4096"


def test_an_absent_output_cap_still_gets_the_runner_ceiling() -> None:
    """A caller that sends nothing is bounded anyway — the cap fails closed."""
    client = _Client()
    engine = DockerSandboxEngine(client)
    engine.ensure(uuid4(), _ensure())

    assert client.containers.values[0].labels[_OUTPUT_CAP_LABEL] == str(_OUTPUT_BYTES_CEILING)


def test_cancel_destroys_the_matching_generation() -> None:
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())
    engine.cancel(session_id, 1, uuid4())
    assert client.containers.values[0].removed is True


def test_docker_exit_137_after_container_removal_is_killed_not_failed() -> None:
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())
    container = client.containers.values[0]
    container.execution_exit_code = 137
    container.stop_on_execute = True

    result = engine.execute_existing(
        session_id,
        ExecuteRequest(generation=1, execution_id=uuid4(), code="while True: pass"),
    )

    assert result["status"] == "killed"
    assert result["exit_code"] is None
    assert "cancelled" in str(result["stderr"])


# --- #519 part 1: model output is STREAMED, not buffered whole -----------------


def test_a_huge_stdout_never_accumulates_beyond_the_cap() -> None:
    """#519's actual claim: bounded PEAK, not merely a bounded return value.

    `exec_run(demux=True)` buffered a process's ENTIRE output before returning, so
    `print("x" * 2_000_000_000)` allocated two gigabytes inside the runner — capped at
    1 GiB in compose — whatever output cap was configured. ADR-0013 §G7 now claims
    "peak is the cap plus one chunk".

    MEASURED, because the obvious assertion cannot see the bug. An earlier version of
    this test asserted only `len(stdout) == cap`, and a review demonstrated that the
    bug-shaped regression — accumulate every chunk, slice once at the join — satisfies
    that and passed the entire suite. Result length says nothing about peak; only
    watching allocation does.

    `tracemalloc` starts AFTER the fixture is built and the chunks are generated
    lazily, so what it measures is what the ENGINE retained while draining, not what
    the test allocated to feed it.
    """
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())
    container = client.containers.values[0]

    chunk_size = 64 * 1024
    chunk_count = 640  # 40 MiB streamed, ~160x the retention cap

    class _LazyChunks:
        """Generates each chunk on demand, so retention is the engine's alone."""

        def __iter__(self) -> Iterator[bytes]:
            for _ in range(chunk_count):
                yield b"x" * chunk_size

    container.stream_chunks = _LazyChunks()  # type: ignore[assignment]

    tracemalloc.start()
    try:
        result = engine.execute_existing(
            session_id,
            ExecuteRequest(
                generation=1,
                execution_id=uuid4(),
                code="print('x' * 2_000_000_000)",
                packages=[],
            ),
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    streamed = chunk_size * chunk_count
    # The bound ADR-0013 §G7 states: the cap plus about one chunk. A generous ceiling
    # (cap + 16 chunks) still fails a buffering implementation by a factor of ~30, so
    # this is decisive without being brittle about interpreter overhead.
    ceiling = _MAX_STREAM_CHARS + 16 * chunk_size
    assert peak < ceiling, (
        f"peak retention {peak} exceeded {ceiling} while draining {streamed} bytes — "
        "the stream is being buffered, not discarded past the cap"
    )
    # …and the returned value is still correct, which is what the old test checked.
    assert len(result["stdout"]) == _MAX_STREAM_CHARS


def test_the_stream_is_drained_past_the_cap_rather_than_abandoned() -> None:
    """Stopping early would hang the model's process, not save memory.

    Closing the stream once the cap is reached leaves the daemon writing into a pipe
    nobody reads, which blocks the process instead of letting it finish — a run that
    printed too much would hang rather than complete with clipped output. So the bytes
    past the cap are read and DROPPED: I/O, but no memory.
    """
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())
    container = client.containers.values[0]
    consumed = 0

    def counting_chunks() -> list[bytes]:
        return [b"y" * 8192] * 200

    container.stream_chunks = counting_chunks()
    total_chunks = len(container.stream_chunks)

    class _Counting(list):
        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal consumed
            for item in list.__iter__(self):
                consumed += 1
                yield item

    container.stream_chunks = _Counting(container.stream_chunks)

    engine.execute_existing(
        session_id,
        ExecuteRequest(
            generation=1, execution_id=uuid4(), code="print('y' * 10_000_000)", packages=[]
        ),
    )

    assert (
        consumed == total_chunks
    ), "the engine stopped reading at the cap, which would block the model's process"
