"""Runner-side limit enforcement (#589): wall clock, scratch, bound reuse."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_engine import _Client, _ensure

from lumen_sandbox_runner.engine import DockerSandboxEngine
from lumen_sandbox_runner.models import ContainerPolicy, EnsureSessionRequest, ExecuteRequest


def test_wall_clock_and_scratch_are_settable_positive_bounds() -> None:
    policy = ContainerPolicy(wall_clock_seconds=30, scratch_bytes=1024 * 1024)
    assert policy.wall_clock_seconds == 30
    assert policy.scratch_bytes == 1024 * 1024
    with pytest.raises(ValidationError):
        ContainerPolicy(wall_clock_seconds=0)
    with pytest.raises(ValidationError):
        ContainerPolicy(scratch_bytes=0)


def test_scratch_bound_reaches_engine_as_tmpfs_volume() -> None:
    client = _Client()
    engine = DockerSandboxEngine(client)
    engine.ensure(
        uuid4(),
        EnsureSessionRequest(
            generation=1,
            image="lumen-sandbox-runner:0.2.0",
            env={"HOME": "/root"},
            container={
                "memory_bytes": 512 * 1024 * 1024,
                "pids_limit": 128,
                "cpus": 1.0,
                "scratch_bytes": 64 * 1024 * 1024,
            },
        ),
    )
    kwargs = client.containers.run_kwargs[0]
    assert kwargs["mem_limit"] == 512 * 1024 * 1024
    assert kwargs["pids_limit"] == 128
    assert kwargs["nano_cpus"] == 1_000_000_000
    # Never a tmpfs MOUNT: Docker's archive API (put_archive/get_archive) cannot see
    # one, so staging code into it failed every bounded execution (#838).
    assert "tmpfs" not in kwargs
    (mount,) = kwargs["mounts"]  # type: ignore[misc]
    assert mount == {
        "Target": "/workspace",
        "Type": "volume",
        "VolumeOptions": {
            "NoCopy": True,
            "DriverConfig": {
                "Name": "local",
                "Options": {
                    "type": "tmpfs",
                    "device": "tmpfs",
                    "o": "size=67108864,nosuid,nodev",
                },
            },
        },
    }


def test_removed_container_takes_its_workspace_volume_with_it() -> None:
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    request = _ensure().model_copy(update={"container": ContainerPolicy(scratch_bytes=1048576)})
    engine.ensure(session_id, request)
    first = client.containers.values[0]
    engine.ensure(session_id, request.model_copy(update={"generation": 2}))
    assert first.removed and first.removed_volumes


def test_reused_container_with_a_tmpfs_workspace_is_recreated() -> None:
    """A container from before #838 (tmpfs at /workspace) never serves a run."""
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    request = _ensure().model_copy(update={"container": ContainerPolicy(scratch_bytes=1048576)})
    engine.ensure(session_id, request)
    stale = client.containers.values[0]
    stale.attrs["HostConfig"]["Mounts"] = []
    stale.attrs["HostConfig"]["Tmpfs"] = {"/workspace": "rw,nosuid,exec,size=1048576"}
    engine.ensure(session_id, request)
    assert stale.removed
    assert len(client.containers.run_kwargs) == 2


def test_reused_container_with_different_bounds_is_recreated() -> None:
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())
    first = client.containers.values[0]
    engine.ensure(
        session_id,
        EnsureSessionRequest(
            generation=1,
            image="lumen-sandbox-runner:0.2.0",
            env={"HOME": "/root"},
            container={"memory_bytes": 256 * 1024 * 1024},
        ),
    )
    assert first.removed is True
    assert len(client.containers.run_kwargs) == 2


def test_infinite_loop_is_killed_at_wall_clock_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream that never yields is cut by the deadline, via an Event handshake.

    The fake stream blocks until the container is removed (what a real
    ``while True`` looks like to the runner: a socket that goes quiet when
    the daemon kills the container). The only way the execution returns is
    the engine's deadline firing and destroying the container.
    """
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())
    container = client.containers.values[0]
    callbacks = []

    class Deadline:
        def __init__(self, seconds, callback):
            assert seconds == 1
            callbacks.append(callback)

        def start(self):
            pass

        def cancel(self):
            pass

        def join(self):
            pass

    monkeypatch.setattr("lumen_sandbox_runner.engine.threading.Timer", Deadline)

    def hang() -> Iterator[bytes]:
        assert callbacks, "deadline must be armed before the stream starts"
        callbacks[0]()
        assert container.removed
        yield b""

    container.stream_chunks = hang()  # type: ignore[assignment]
    result = engine.execute_existing(
        session_id,
        ExecuteRequest(
            generation=1,
            execution_id=uuid4(),
            code="while True: pass",
            wall_clock_seconds=1,
        ),
    )
    assert result["status"] == "timeout"
    assert result["termination_reason"] == "sandbox_limit_wall_clock"
    assert container.removed is True


def test_oom_state_is_classified_as_memory_limit() -> None:
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())
    container = client.containers.values[0]
    container.execution_exit_code = 137
    container.attrs["State"] = {"OOMKilled": True}
    result = engine.execute_existing(
        session_id,
        ExecuteRequest(generation=1, execution_id=uuid4(), code="x = bytearray(10**9)"),
    )
    assert result["status"] == "killed"
    assert result["termination_reason"] == "sandbox_limit_memory"


@pytest.mark.parametrize(
    "message,reason",
    [
        ("OSError: [Errno 28] No space left on device", "sandbox_limit_scratch"),
        ("BlockingIOError: [Errno 11] Resource temporarily unavailable", "sandbox_limit_pids"),
        ("MemoryError", "sandbox_limit_memory"),
    ],
)
def test_resource_overages_have_distinct_reasons_and_cleanup(message, reason) -> None:
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())
    container = client.containers.values[0]
    container.execution_exit_code = 1
    container._run_model_code = lambda: type(
        "Outcome", (), {"exit_code": 1, "output": (b"", message.encode())}
    )()
    result = engine.execute_existing(
        session_id, ExecuteRequest(generation=1, execution_id=uuid4(), code="pass")
    )
    assert result["termination_reason"] == reason
    assert result["status"] == "failed"
    assert container.removed


def test_output_overage_is_a_terminal_limit_failure() -> None:
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure(output_bytes_cap=1024))
    container = client.containers.values[0]
    container.output_files = [("big.txt", 2048)]
    result = engine.execute_existing(
        session_id, ExecuteRequest(generation=1, execution_id=uuid4(), code="pass")
    )
    assert result["termination_reason"] == "sandbox_limit_output"
    assert result["status"] == "failed"
    assert container.removed


def test_live_hostconfig_mismatch_is_recreated_even_with_matching_label() -> None:
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    request = _ensure().model_copy(
        update={"container": ContainerPolicy(memory_bytes=128 * 1024 * 1024)}
    )
    engine.ensure(session_id, request)
    container = client.containers.values[0]
    container.attrs["HostConfig"]["Memory"] = 0
    engine.ensure(session_id, request)
    assert container.removed
    assert len(client.containers.run_kwargs) == 2


def test_scratch_bound_cannot_be_bypassed_via_writable_root() -> None:
    client = _Client()
    engine = DockerSandboxEngine(client)
    engine.ensure(
        uuid4(), _ensure().model_copy(update={"container": ContainerPolicy(scratch_bytes=1048576)})
    )
    kwargs = client.containers.run_kwargs[0]
    assert kwargs["read_only"] is True
    assert [m["Target"] for m in kwargs["mounts"]] == ["/workspace"]  # type: ignore[attr-defined]


def test_expired_queue_deadline_never_starts_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lumen_sandbox_runner.engine.time.time", lambda: 100.0)
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())
    container = client.containers.values[0]
    result = engine.execute_existing(
        session_id,
        ExecuteRequest(
            generation=1, execution_id=uuid4(), code="while True: pass", deadline_unix=99.0
        ),
    )
    assert result["termination_reason"] == "sandbox_limit_wall_clock"
    assert container.python_calls == 0
    assert container.removed
