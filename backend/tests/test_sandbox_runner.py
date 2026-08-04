"""HTTP reusable-sandbox runner client tests (ADR-0020, issue #457)."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from app.core.errors import DependencyError
from app.domain.code_execution import (
    SANDBOX_REASON_MESSAGES,
    SANDBOX_REASON_PUBLIC_MESSAGES,
    SANDBOX_REASON_RUNNER_ERROR,
    SANDBOX_REASON_RUNNER_REJECTED,
    SANDBOX_REASON_RUNNER_UNAUTHORIZED,
    SANDBOX_REASON_RUNNER_UNAVAILABLE,
)
from app.domain.entities import CodeRunStatus
from app.sandbox.runner import (
    RUNNER_TOKEN_HEADER,
    HttpSandboxRunner,
    SandboxRunnerFailed,
    SandboxRunnerRejected,
    SandboxRunnerUnauthorized,
    SandboxRunnerUnavailable,
)
from app.sandbox.spec import RunSpec, SandboxSessionSpec


def _session(*, generation: int = 1) -> SandboxSessionSpec:
    return SandboxSessionSpec(
        sandbox_session_id=uuid4(),
        generation=generation,
        image="python@sha256:abc",
        runtime="runsc",
        output_bytes_cap=4 * 1024 * 1024,
        env=(("HOME", "/root"),),
    )


async def test_execute_ensures_root_writable_unbounded_session_then_runs() -> None:
    requests: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append((request.method, request.url.path, body))
        if request.method == "PUT":
            return httpx.Response(200, json={"status": "active"})
        return httpx.Response(
            200,
            json={
                "status": "succeeded",
                "exit_code": 0,
                "stdout": "hi\n",
                "stderr": "",
                "duration_ms": 7,
                "image_digest": "sha256:abc",
                "output_files": [],
            },
        )

    value = _session()
    runner = HttpSandboxRunner("http://sandbox-runner:8000", transport=httpx.MockTransport(handler))
    result = await runner.execute(
        value,
        RunSpec(code="print('hi')", packages=("numpy==2.1.0",)),
    )

    assert result.status is CodeRunStatus.SUCCEEDED
    assert result.stdout == "hi\n"
    assert [request[0] for request in requests] == ["PUT", "POST"]
    session_body = requests[0][2]
    container = session_body["container"]
    assert isinstance(container, dict)
    assert container == {
        "network_mode": "none",
        "binds": [],
        "read_only_rootfs": False,
        "user": "0:0",
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "runtime": "runsc",
        "cpus": None,
        "memory_bytes": None,
        "pids_limit": None,
        "wall_clock_seconds": None,
        # The one bound that IS sent: the runner must not read an unbounded amount of
        # a model-controlled output directory into the memory of the single
        # Docker-socket holder. It was `None` here, and dead on the runner side too.
        "output_bytes_cap": 4 * 1024 * 1024,
    }
    assert session_body["env"] == {"HOME": "/root"}
    assert requests[1][2]["packages"] == ["numpy==2.1.0"]


async def test_reset_closes_old_generation_before_ensuring_new_generation() -> None:
    calls: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.url.query.decode()))
        return httpx.Response(200, json={"status": "active"})

    first = _session(generation=1)
    second = SandboxSessionSpec(
        sandbox_session_id=first.sandbox_session_id,
        generation=2,
        image=first.image,
        runtime=first.runtime,
    )
    runner = HttpSandboxRunner("http://sandbox-runner:8000", transport=httpx.MockTransport(handler))
    await runner.reset_session(first, second)

    assert calls == [
        ("DELETE", f"/sessions/{first.sandbox_session_id}", "generation=1"),
        ("PUT", f"/sessions/{first.sandbox_session_id}", ""),
    ]


async def test_unreachable_runner_raises_dependency_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    runner = HttpSandboxRunner("http://sandbox-runner:8000", transport=httpx.MockTransport(handler))
    with pytest.raises(SandboxRunnerUnavailable) as refusal:
        await runner.execute(_session(), RunSpec(code="print(1)"))
    # Still a DependencyError for every existing handler; now also typed.
    assert isinstance(refusal.value, DependencyError)
    assert refusal.value.sandbox_reason == SANDBOX_REASON_RUNNER_UNAVAILABLE


def _answering(status: int, detail: str) -> httpx.MockTransport:
    """A runner that IS up and answers ``status`` — the opposite of unreachable."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(200, json={"status": "active"})
        return httpx.Response(status, json={"detail": detail})

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("status", "detail"),
    [
        (422, "an approved package could not be downloaded"),
        (422, "invalid package requirement"),
        (409, "sandbox session must be ensured before execution"),
        (404, "sandbox session not found"),
    ],
)
async def test_a_runner_that_answers_4xx_is_not_reported_as_unreachable(
    status: int, detail: str
) -> None:
    """Issue #502 — every HTTP error used to collapse into "runner unavailable".

    A failed package download, an invalid requirement and a stale generation are
    all answers from a **running** service. Reporting them as "unreachable" sent an
    operator to restart a container that was serving every request correctly.
    """
    runner = HttpSandboxRunner("http://sandbox-runner:8000", transport=_answering(status, detail))
    with pytest.raises(SandboxRunnerRejected) as refusal:
        await runner.execute(_session(), RunSpec(code="print(1)", packages=("numpy",)))

    assert refusal.value.sandbox_reason == SANDBOX_REASON_RUNNER_REJECTED
    assert refusal.value.sandbox_reason != SANDBOX_REASON_RUNNER_UNAVAILABLE
    assert not isinstance(refusal.value, SandboxRunnerUnavailable)
    # Over HTTP it is still a 503: the lifecycle endpoints' caller sent a valid
    # request and the sandbox contract declares 404/409/503, so the runner refusing
    # must not surface as the caller's own validation error.
    assert refusal.value.status == 503
    # The runner's own sentence names host-side facts and this error renders into an
    # HTTP problem body, so it is logged — never carried in ``detail``.
    assert detail not in (refusal.value.detail or "")


async def test_a_runner_that_answers_5xx_is_distinct_from_both() -> None:
    """A reachable-but-broken runner (e.g. the execution image is missing)."""
    runner = HttpSandboxRunner(
        "http://sandbox-runner:8000",
        transport=_answering(503, "sandbox execution image is not present on the host daemon"),
    )
    with pytest.raises(SandboxRunnerFailed) as refusal:
        await runner.execute(_session(), RunSpec(code="print(1)"))

    assert refusal.value.sandbox_reason == SANDBOX_REASON_RUNNER_ERROR
    assert not isinstance(refusal.value, SandboxRunnerUnavailable)
    assert not isinstance(refusal.value, SandboxRunnerRejected)


async def test_the_three_runner_outcomes_are_pairwise_distinct() -> None:
    """The point of the split: one code per remedy, derived from real calls."""
    reasons: set[str] = set()
    for transport, expected in (
        (_answering(422, "invalid package requirement"), SandboxRunnerRejected),
        (_answering(500, "sandbox execution failed"), SandboxRunnerFailed),
    ):
        runner = HttpSandboxRunner("http://sandbox-runner:8000", transport=transport)
        with pytest.raises(expected) as refusal:
            await runner.execute(_session(), RunSpec(code="print(1)"))
        reasons.add(refusal.value.sandbox_reason)

    def _refuse_connection(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    down = HttpSandboxRunner(
        "http://sandbox-runner:8000", transport=httpx.MockTransport(_refuse_connection)
    )
    with pytest.raises(SandboxRunnerUnavailable) as unreachable:
        await down.execute(_session(), RunSpec(code="print(1)"))
    reasons.add(unreachable.value.sandbox_reason)

    assert len(reasons) == 3


def test_malformed_status_fails_closed() -> None:
    from app.sandbox.runner import _map_status

    assert _map_status("succeeded") is CodeRunStatus.SUCCEEDED
    assert _map_status("nonsense") is CodeRunStatus.FAILED
    assert _map_status(None) is CodeRunStatus.FAILED


# --- #508: the runner authenticates, and a 401 is its own remedy ---------------


async def test_the_client_presents_the_shared_secret_on_every_request() -> None:
    """The runner holds the Docker socket and now authenticates every endpoint.

    Asserted across a MULTI-request operation (`execute` does a PUT then a POST)
    because the header is set on the client, not per call site — and the whole point
    of putting it there is that a newly added request cannot forget it.
    """
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(RUNNER_TOKEN_HEADER))
        if request.url.path.endswith("/executions"):
            return httpx.Response(
                200,
                json={
                    "status": "succeeded",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "duration_ms": 1,
                    "image_digest": "sha256:abc",
                    "output_files": [],
                },
            )
        return httpx.Response(200, json={"status": "active"})

    runner = HttpSandboxRunner(
        "http://sandbox-runner:8000",
        token="s" * 48,
        transport=httpx.MockTransport(handler),
    )
    await runner.execute(_session(), RunSpec(code="print(1)"))

    assert len(seen) >= 2, "expected at least the ensure + execute requests"
    assert set(seen) == {"s" * 48}


async def test_a_401_is_its_own_reason_not_a_generic_rejection() -> None:
    """ "Wrong credential" and "refused your request" need different actions (#508).

    A rejection means the runner examined a well-formed, authenticated request and
    declined it — an unresolvable package, a stale generation — so the operator reads
    the runner's log looking for the request. A 401 means it never got that far: the
    service is up, reachable and working, and only the secret is wrong. Collapsing
    them would send an operator hunting a package failure that never happened.
    """
    runner = HttpSandboxRunner(
        "http://sandbox-runner:8000",
        token="wrong-token-that-is-long-enough-to-pass",
        transport=_answering(401, "sandbox runner token missing or invalid"),
    )
    with pytest.raises(SandboxRunnerUnauthorized) as refusal:
        await runner.execute(_session(), RunSpec(code="print(1)"))

    assert refusal.value.sandbox_reason == SANDBOX_REASON_RUNNER_UNAUTHORIZED
    # Distinct from all three pre-existing outcomes, not merely a new label on one.
    assert not isinstance(refusal.value, SandboxRunnerRejected)
    assert not isinstance(refusal.value, SandboxRunnerUnavailable)
    assert not isinstance(refusal.value, SandboxRunnerFailed)
    # Still a DependencyError → 503: the caller's request was fine.
    assert refusal.value.status == 503


def test_the_operator_sentence_names_the_variable_and_the_public_one_does_not() -> None:
    """The two-audience split spec 0004 §2.4 requires, applied to the new reason.

    The operator sentence may name `SANDBOX_RUNNER_TOKEN` — it goes to the log, to
    `audit_events.metadata` and to the runbook, all behind an operator boundary. The
    public sentence reaches the model and the transcript, so it must name no
    variable, no service and no topology: a user cannot fix a shared secret, and
    telling them its name only discloses deployment shape.
    """
    operator = SANDBOX_REASON_MESSAGES[SANDBOX_REASON_RUNNER_UNAUTHORIZED]
    public = SANDBOX_REASON_PUBLIC_MESSAGES[SANDBOX_REASON_RUNNER_UNAUTHORIZED]

    assert "SANDBOX_RUNNER_TOKEN" in operator
    for leak in ("SANDBOX_RUNNER_TOKEN", "sandbox-runner", "401", "secret", "token"):
        assert leak not in public
