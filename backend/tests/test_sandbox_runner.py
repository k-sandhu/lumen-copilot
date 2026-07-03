"""HttpSandboxRunner tests — the internal-API client (ADR-0013 §1, #230).

All offline: the ``sandbox-runner`` internal HTTP API is driven by an
``httpx.MockTransport``, so the client's POST-then-poll loop, its domain-mapping,
its fail-closed status handling, and its unreachable-runner → ``DependencyError``
path are exercised without any network or container engine.

The worker's ONLY container-engine surface is this HTTP hop (ADR-0013 §1) — it holds
no Docker socket. The client posts the enforced container flags as data so the runner
applies exactly the hardening the ``sandbox/`` module decided (asserted here).
"""

from __future__ import annotations

import httpx
import pytest

from app.core.errors import DependencyError
from app.domain.entities import CodeRunStatus
from app.sandbox.runner import HttpSandboxRunner
from app.sandbox.spec import RunLimits, RunSpec

_LIMITS = RunLimits(
    cpus=1.0, memory_bytes=1024, pids=8, wall_clock_seconds=5, output_bytes_cap=1024
)


def _spec() -> RunSpec:
    return RunSpec(code="print('hi')", limits=_LIMITS)


async def test_run_posts_enforced_flags_and_maps_result() -> None:
    """A completed run maps to a domain RunResult; the POST carries the enforced flags."""
    posted: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            import json

            posted.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "run-1"})
        return httpx.Response(
            200,
            json={
                "id": "run-1",
                "status": "succeeded",
                "exit_code": 0,
                "stdout": "hi\n",
                "stderr": "",
                "duration_ms": 7,
                "image_digest": "sha256:abc",
                "output_files": [
                    {"filename": "o.csv", "content_type": "text/csv", "data_b64": "YSxi"}
                ],
                "resource_usage": {"peak_memory_bytes": 2048, "output_bytes": 3},
            },
        )

    runner = HttpSandboxRunner("http://sandbox-runner:8000", transport=httpx.MockTransport(handler))
    result = await runner.run(_spec(), tmpfs_scratch_bytes=1024)

    assert result.status is CodeRunStatus.SUCCEEDED
    assert result.exit_code == 0
    assert result.stdout == "hi\n"
    assert result.image_digest == "sha256:abc"
    assert len(result.output_files) == 1
    assert result.output_files[0].data == b"a,b"
    assert result.resource_usage is not None
    # The POST body carried the ENFORCED isolation flags (G1/G2/G6) as data.
    container = posted["container"]
    assert isinstance(container, dict)
    assert container["network_mode"] == "none"  # G2/G3
    assert container["read_only_rootfs"] is True  # G1
    assert container["cap_drop"] == ["ALL"]
    assert container["memory_bytes"] == 1024  # G6
    assert container["output_bytes_cap"] == 1024  # G7
    # No host env is forwarded (G5): only the run's explicit env (empty here).
    assert posted["env"] == {}


async def test_unreachable_runner_raises_dependency_error() -> None:
    """A runner that errors surfaces as a typed DependencyError (the service → failed)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    runner = HttpSandboxRunner("http://sandbox-runner:8000", transport=httpx.MockTransport(handler))
    with pytest.raises(DependencyError):
        await runner.run(_spec(), tmpfs_scratch_bytes=1024)


async def test_malformed_status_is_failed_closed() -> None:
    """An unknown runner status maps to ``failed`` — a bad response never looks successful."""
    from app.sandbox.runner import _map_status

    assert _map_status("succeeded") is CodeRunStatus.SUCCEEDED
    assert _map_status("nonsense") is CodeRunStatus.FAILED
    assert _map_status(None) is CodeRunStatus.FAILED
