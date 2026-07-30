from __future__ import annotations

import base64
import builtins
import io
import tarfile
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from lumen_sandbox_runner.engine import (
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
        self.image = _Image()
        self.attrs = {"Config": {"Env": []}}
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
        self.archive_chunk_size = 256
        self.archive_chunks_yielded = 0
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
        if command[:4] == ["python", "-m", "pip", "install"]:
            self.events.append("pip-install")
        elif command and command[0] == "python":
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
        return SimpleNamespace(exit_code=0, output=(b"", b""))

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
            for chunk in chunks:
                self.archive_chunks_yielded += 1
                yield chunk

        return _stream(), {}


class _ImageNotFound(Exception):
    """Stands in for ``docker.errors.ImageNotFound``."""


class _Images:
    """The host daemon's local image store, plus the pull docker-py would perform."""

    def __init__(self) -> None:
        self.present = {"lumen-sandbox-runner:0.2.0", "lumen-sandbox-exec:0.1.0"}
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


class _Client:
    def __init__(self) -> None:
        self.containers = _Containers()
        self.images = _Images()


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
        ContainerPolicy(memory_bytes=1024)  # type: ignore[arg-type]
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
