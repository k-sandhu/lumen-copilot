from __future__ import annotations

import builtins
import io
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from lumen_sandbox_runner.engine import DockerSandboxEngine, RunnerError
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

    def get_archive(self, path: str) -> tuple[list[bytes], dict[str, object]]:
        del path
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w"):
            pass
        return [data.getvalue()], {}


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


def _ensure(*, generation: int = 1) -> EnsureSessionRequest:
    return EnsureSessionRequest(
        generation=generation,
        image="lumen-sandbox-runner:0.2.0",
        env={"HOME": "/root"},
        container={"runtime": "runc"},
    )


def test_wire_policy_rejects_any_isolation_widening() -> None:
    with pytest.raises(ValidationError):
        ContainerPolicy(network_mode="bridge")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ContainerPolicy(binds=["/host:/workspace"])
    with pytest.raises(ValidationError):
        ContainerPolicy(memory_bytes=1024)  # type: ignore[arg-type]
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


def test_runtime_is_runner_side_config_and_ignores_the_request() -> None:
    """A gVisor deploy cannot be downgraded per session by the caller.

    ``EnsureSessionRequest`` carries ``runtime`` for wire compatibility, but the whole
    safety argument of a gVisor deploy rests on the sandbox host's configuration — so
    that is where the value comes from.
    """
    client = _Client()
    engine = DockerSandboxEngine(client, runtime="runsc")

    engine.ensure(uuid4(), _ensure())  # the request asks for plain runc

    assert client.containers.run_kwargs[0]["runtime"] == "runsc"


def test_runtime_defaults_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SANDBOX_RUNTIME`` on the runner service is the single source of truth."""
    monkeypatch.setenv("SANDBOX_RUNTIME", "runsc")
    client = _Client()

    DockerSandboxEngine(client).ensure(uuid4(), _ensure())

    assert client.containers.run_kwargs[0]["runtime"] == "runsc"


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
