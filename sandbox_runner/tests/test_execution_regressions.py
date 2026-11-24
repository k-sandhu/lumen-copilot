"""R1 regressions for execution lifetime and reusable package targets (#589)."""

from __future__ import annotations

import csv
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_engine import _Client, _ensure

from lumen_sandbox_runner.engine import DockerSandboxEngine
from lumen_sandbox_runner.models import ExecuteRequest


@pytest.fixture
def deadlines(monkeypatch):
    timers = []

    class Timer:
        def __init__(self, seconds, callback):
            self.callback = callback
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

        def join(self):
            pass

    monkeypatch.setattr("lumen_sandbox_runner.engine.threading.Timer", Timer)
    return timers


def test_detached_child_is_stopped_before_collection_and_admission_release(monkeypatch, deadlines):
    client = _Client()
    engine = DockerSandboxEngine(client)
    session = uuid4()
    engine.ensure(session, _ensure())
    container = client.containers.values[0]
    child = {"alive": False}
    original_exec = container.exec_run
    original_archive = container.get_archive

    def exec_run(command, **kwargs):
        if command[:4] == ["python3", "-I", "-S", "-c"]:
            if "os.kill" in command[4]:
                assert child["alive"]
                assert not deadlines[0].cancelled
                child["alive"] = False
                return SimpleNamespace(exit_code=0, output=(b"", b""))
            return SimpleNamespace(exit_code=0, output=(b'{"1": "init"}', b""))
        return original_exec(command, **kwargs)

    def run_model():
        child["alive"] = True  # detached process has no inherited stream descriptors
        return SimpleNamespace(exit_code=0, output=(b"parent finished\n", b""))

    def archive(path):
        assert not child["alive"], "detached work must stop before outputs/admission settle"
        return original_archive(path)

    monkeypatch.setattr(container, "exec_run", exec_run)
    monkeypatch.setattr(container, "_run_model_code", run_model)
    monkeypatch.setattr(container, "get_archive", archive)
    code = (
        "import subprocess\n"
        "subprocess.Popen(['python', '-c', 'while True: pass'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        "start_new_session=True)"
    )
    result = engine.execute_existing(
        session,
        ExecuteRequest(generation=1, execution_id=uuid4(), code=code, wall_clock_seconds=30),
    )
    assert result["status"] == "succeeded"
    assert not child["alive"]
    assert not container.removed, "session files survive normal descendant cleanup"


def test_blocked_download_is_killed_and_reaped_by_execution_deadline(monkeypatch, deadlines):
    client = _Client()
    engine = DockerSandboxEngine(client)
    session = uuid4()
    engine.ensure(session, _ensure())
    container = client.containers.values[0]
    events = []

    class Process:
        pid = 4242
        returncode = None

        def __init__(self, command, **kwargs):
            assert kwargs["start_new_session"] is True
            events.append("spawn")

        def wait(self, timeout=None):
            if self.returncode is None:
                assert timeout is not None, "acquisition receives the remaining deadline"
                deadlines[0].callback()
                assert "kill-group" in events, "expiry must unblock the downloader itself"
                self.returncode = -9
            events.append("reap")
            return self.returncode

    def kill_group(pid, signum):
        assert pid == 4242
        events.append("kill-group")

    def unbounded_download(*args, **kwargs):
        pytest.fail("package acquisition still uses an uninterruptible subprocess.run")

    monkeypatch.setattr(subprocess, "Popen", Process)
    monkeypatch.setattr(subprocess, "run", unbounded_download)
    monkeypatch.setattr(os, "killpg", kill_group, raising=False)
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    result = engine.execute_existing(
        session,
        ExecuteRequest(
            generation=1,
            execution_id=uuid4(),
            code="pass",
            packages=["reviewdemo"],
            wall_clock_seconds=30,
        ),
    )
    assert result["status"] == "timeout"
    assert result["termination_reason"] == "sandbox_limit_wall_clock"
    assert events.index("kill-group") < events.index("reap")
    assert container.python_calls == 0
    assert container.removed


def _wheel(destination: Path, version: str) -> None:
    metadata = f"reviewdemo-{version}.dist-info"
    files = {
        "reviewdemo/__init__.py": f"__version__ = '{version}'\n",
        f"{metadata}/METADATA": f"Metadata-Version: 2.1\nName: reviewdemo\nVersion: {version}\n",
        f"{metadata}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    if version == "1.0":
        files["reviewdemo/obsolete.py"] = "old = True\n"
    record = io.StringIO()
    csv.writer(record).writerows((path, "", "") for path in [*files, f"{metadata}/RECORD"])
    files[f"{metadata}/RECORD"] = record.getvalue()
    with zipfile.ZipFile(destination / f"reviewdemo-{version}-py3-none-any.whl", "w") as archive:
        for path, value in files.items():
            archive.writestr(path, value)


@pytest.mark.parametrize("scratch", [None, 1048576], ids=["opt-out", "bounded"])
def test_reused_install_import_and_resolution_agree(tmp_path, monkeypatch, scratch):
    """Execute real offline wheels/pip at the container seam, with no daemon/network."""
    client = _Client()

    def download(packages, destination):
        _wheel(destination, packages[0].split("==")[1])

    engine = DockerSandboxEngine(client, package_downloader=download)
    session = uuid4()
    policy = _ensure()
    policy.container.scratch_bytes = scratch
    engine.ensure(session, policy)
    container = client.containers.values[0]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_exec = container.exec_run

    def translated(value):
        return value.replace("/workspace", str(workspace).replace("\\", "/"))

    def exec_run(command, **kwargs):
        if command[:4] in (["python", "-m", "pip", "install"], ["python3", "-I", "-S", "-c"]):
            command = [sys._base_executable, *(translated(value) for value in command[1:])]
            outcome = subprocess.run(command, capture_output=True, timeout=30)
            return SimpleNamespace(
                exit_code=outcome.returncode, output=(outcome.stdout, outcome.stderr)
            )
        return original_exec(command, **kwargs)

    def put_archive(path, data):
        target = Path(translated(path))
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(target, filter="data")
        return True

    def run_model():
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.update(
            {
                key: translated(value)
                for key, value in client.containers.run_kwargs[0]["environment"].items()
            }
        )
        outcome = subprocess.run(
            [sys._base_executable, "-c", "import reviewdemo; print(reviewdemo.__version__)"],
            env=environment,
            capture_output=True,
            timeout=30,
        )
        return SimpleNamespace(
            exit_code=outcome.returncode, output=(outcome.stdout, outcome.stderr)
        )

    monkeypatch.setattr(container, "exec_run", exec_run)
    monkeypatch.setattr(container, "put_archive", put_archive)
    monkeypatch.setattr(container, "_run_model_code", run_model)
    # Process-namespace cleanup is tested separately; this seam runs on the host.
    monkeypatch.setattr(engine, "_snapshot_processes", lambda container: {}, raising=False)
    monkeypatch.setattr(
        engine, "_stop_execution_processes", lambda container, baseline: None, raising=False
    )
    for version in ("1.0", "2.0"):
        result = engine.execute_existing(
            session,
            ExecuteRequest(
                generation=1,
                execution_id=uuid4(),
                code="import reviewdemo",
                packages=[f"reviewdemo=={version}"],
            ),
        )
        assert result["status"] == "succeeded", result["stderr"]
        assert str(result["stdout"]).strip() == version
        assert result["resolved_packages"][0]["version"] == version
        if version == "1.0":
            # A second distribution can contribute to the same package namespace.
            # Replacing reviewdemo must preserve that distribution's files.
            addon = workspace / ".packages" / "reviewdemo" / "addon.py"
            addon.write_text("installed_by_another_distribution = True\n")
    target = workspace / ".packages"
    assert sorted(path.name for path in target.glob("*.dist-info")) == ["reviewdemo-2.0.dist-info"]
    assert not (target / "reviewdemo" / "obsolete.py").exists()
    assert (target / "reviewdemo" / "addon.py").exists()


def test_cleanup_script_stops_reparented_descendants_and_detects_pid_reuse(tmp_path, monkeypatch):
    from lumen_sandbox_runner.commands import PROCESS_CLEANUP

    proc = tmp_path / "proc"

    def process(pid, starttime):
        path = proc / str(pid) / "stat"
        path.parent.mkdir(parents=True)
        path.write_text(f"{pid} (detached child) S " + "0 " * 18 + starttime)

    process(1, "init")
    process(20, "idle")
    process(30, "reused")
    process(40, "detached")
    killed = []

    def kill(pid, signum):
        assert signum == 9
        killed.append(pid)
        (proc / str(pid) / "stat").unlink()

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(
        sys, "argv", ["cleanup", json.dumps({"1": "init", "20": "idle", "30": "old"})]
    )
    exec(PROCESS_CLEANUP.replace("'/proc'", repr(str(proc).replace("\\", "/"))), {})
    assert sorted(killed) == [30, 40]
    assert (proc / "20" / "stat").exists()


def test_collection_permit_wait_cannot_outlive_execution(monkeypatch, deadlines):
    client = _Client()
    engine = DockerSandboxEngine(client)
    session = uuid4()
    engine.ensure(session, _ensure())
    container = client.containers.values[0]

    class Permit:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def acquire(self, timeout=None):
            assert timeout is not None
            deadlines[0].callback()
            return False

        def release(self):
            pytest.fail("an unacquired permit must not be released")

    monkeypatch.setattr("lumen_sandbox_runner.engine._collection_permits", Permit())
    result = engine.execute_existing(
        session,
        ExecuteRequest(generation=1, execution_id=uuid4(), code="pass", wall_clock_seconds=30),
    )
    assert result["termination_reason"] == "sandbox_limit_wall_clock"
    assert container.removed


def test_archive_deadline_interrupts_socket_and_closes_response():
    from lumen_sandbox_runner.execution import ExecutionDeadline, current_deadline
    from lumen_sandbox_runner.transport import DeadlineAPIClient

    deadline = ExecutionDeadline(30)
    events = []
    sock = SimpleNamespace(
        settimeout=lambda value: events.append("timeout"),
        shutdown=lambda how: events.append("shutdown"),
    )

    class Response:
        def iter_content(self, size, decode):
            deadline.expire()
            assert "shutdown" in events, "expiry must interrupt a quiet/trickling socket"
            yield b""

        def close(self):
            events.append("close")

    api = DeadlineAPIClient.__new__(DeadlineAPIClient)
    api._get_raw_response_socket = lambda response: sock
    token = current_deadline.set(deadline)
    try:
        with pytest.raises(TimeoutError):
            list(api._stream_raw_result(Response()))
    finally:
        current_deadline.reset(token)
    assert events == ["timeout", "shutdown", "close"]
