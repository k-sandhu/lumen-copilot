"""Live sandbox execution — real Python, in a real container (issue #506).

**Why this file exists.** Every other sandbox test in this suite substitutes
something for reality: ``tests/test_sandbox_runner.py`` drives an
``httpx.MockTransport``, ``tests/test_sandbox_service.py`` injects a fake
``SandboxRunner``, ``sandbox_runner/tests/`` hands the engine a fake Docker client,
and ``tests/test_sandbox_exec_image.py`` reads the Dockerfile as *text*. Not one of
them ever ran a line of Python inside a container. That is precisely why #501–#505
all shipped dark: a green suite proved the wiring was self-consistent, never that
``run_python`` produced output.

**The seam under test.** :class:`~app.sandbox.runner.HttpSandboxRunner` — the
production client the Celery worker uses — against the real ``sandbox-runner``
service, which drives the real host Docker daemon, which launches the real
``lumen-sandbox-exec`` image. Everything from the ``RunSpec`` down is genuine;
nothing is faked.

**What it deliberately does NOT cover** (no overclaiming): the policy layers
*above* the runner — the tool-policy gate (#501), the four refusal reasons (#502),
package admission (#504) and the assistant allow-list (#505) — are exercised by
their own offline tests, and a run here is not persisted as a ``code_runs`` row.
This file answers exactly one question those cannot: *does model-authored Python
actually execute, and does its output come back?*

**Gating** (issue #94 parity, mirroring ``test_realtime_backplane_live.py`` and
``tests/eval/test_chat_latency_live.py``). It creates and destroys real containers,
so it is doubly off by default:

* an explicit ``RUN_LIVE=1`` opt-in — a **strict** string compare, because a lenient
  truthy parse that accepted ``"true"``/``"2"``/``"no"`` was a real money bug on the
  token-spending harness (R2-5); and
* a reachable ``sandbox-runner``, probed **inside a fixture** so an offline
  collection opens no socket at all.

Absent either, the tests SKIP with a reason that names what is missing — they never
pass silently. A runner that IS reachable but cannot launch the execution image is a
**failure**, not a skip: that is the #503 symptom and it must be loud.

Run it with the sandbox profile up and the execution image built
(see ``docs/runbooks/sandbox-code-execution.md`` §6)::

    docker compose --profile sandbox build sandbox-exec-image
    docker compose --profile sandbox up -d sandbox-runner
    docker compose exec backend sh -lc \\
      "UV_PROJECT_ENVIRONMENT=/tmp/lumen-dev-venv RUN_LIVE=1 \\
       uv run --frozen --extra dev pytest -q -p no:cacheprovider \\
       tests/test_sandbox_execution_live.py"

It runs from *inside* the backend container because ADR-0020 §2 gives the runner **no
published host port** — the compose network is the only way in, exactly as the worker
reaches it. ``UV_PROJECT_ENVIRONMENT`` and ``-p no:cacheprovider`` keep the dev venv
and the pytest cache out of the bind-mounted ``/app`` (where they would collide with
the host checkout's own). A host-side run works too if ``SANDBOX_RUNNER_URL`` is
pointed at a forwarded port.
"""

from __future__ import annotations

import os
import re
import textwrap
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import DependencyError
from app.domain.entities import CodeRunStatus
from app.sandbox.runner import HttpSandboxRunner
from app.sandbox.service import SandboxSessionService
from app.sandbox.spec import RunResult, RunSpec, SandboxSessionSpec

# STRICT (R2-5): only the exact string "1" opts in. Everything else — including
# "true", "2" and a typo'd "no" — leaves the gate closed. This is a pure env read;
# the reachability probe lives in the fixture, so importing this module opens no
# socket (the offline guard in ``test_sandbox_execution_gating.py`` pins that).
_RUN_LIVE = os.environ.get("RUN_LIVE") == "1"

_live = pytest.mark.skipif(
    not _RUN_LIVE,
    reason=(
        "live sandbox execution opted out: needs RUN_LIVE=1 exactly AND a reachable "
        f"sandbox-runner (RUN_LIVE={'=1' if _RUN_LIVE else 'not 1'}) — skipped "
        "(offline-safe: no socket, no container)."
    ),
)

#: How long the readiness probe waits for ``GET /health``. Deliberately short: this
#: only decides skip-vs-run, and a slow answer here means the service is not up.
_PROBE_TIMEOUT_SECONDS = 5.0

#: The build command a missing execution image needs. Named in the failure message
#: because "sandbox runner unavailable" is otherwise indistinguishable from a down
#: service, and #503 is exactly the case where the image is the thing that is wrong.
_BUILD_COMMAND = "docker compose --profile sandbox build sandbox-exec-image"


@dataclass(frozen=True, slots=True)
class _LiveSandbox:
    """One ensured, live sandbox generation plus the client that reaches it."""

    runner: HttpSandboxRunner
    spec: SandboxSessionSpec


async def _probe(url: str) -> None:
    """Skip (loudly) unless the runner answers its health endpoint.

    A ``GET /health`` rather than a bare TCP connect: the runner is the only thing
    that can launch a container, and a socket that merely accepts (a stray process,
    a port-forward to nothing) would let the suite look like it ran when it did not.
    """
    async with httpx.AsyncClient(base_url=url, timeout=_PROBE_TIMEOUT_SECONDS) as probe:
        response = await probe.get("/health")
        response.raise_for_status()


@pytest.fixture
async def sandbox() -> AsyncIterator[_LiveSandbox]:
    """Ensure a real sandbox container for one test and destroy it afterwards.

    Function-scoped on purpose: each test gets a pristine generation, so no test can
    pass on state another one left behind, and a failure cannot leak a container into
    the next. The identity is a fresh ``uuid4``, so a live run never collides with a
    real chat's sandbox.

    Skips when opted in but the stack is not usable (unreadable ``Settings``, or a
    ``sandbox-runner`` that does not answer). **Fails** — never skips — when the
    runner is up but cannot start the image: that is the #503 defect, and a skip
    there would hide the very thing this file exists to catch.
    """
    try:
        settings = Settings()  # validates the whole stack config (DB/Redis/S3/…)
    except Exception as exc:  # noqa: BLE001 — incomplete env → skip, not error
        pytest.skip(f"Settings unavailable for the live sandbox test: {type(exc).__name__}")

    url = settings.sandbox_runner_url
    try:
        await _probe(url)
    except Exception as exc:  # noqa: BLE001 — any transport failure → skip, not error
        pytest.skip(
            f"sandbox-runner unreachable at {url!r} ({type(exc).__name__}) — opted in via "
            "RUN_LIVE=1 but the service is not up. Start it with "
            "`docker compose --profile sandbox up -d sandbox-runner`, and run this file "
            "from inside the backend container (ADR-0020 §2: the runner publishes no host "
            "port) or point SANDBOX_RUNNER_URL at a forwarded one."
        )

    runner = HttpSandboxRunner(url)
    spec = SandboxSessionSpec(
        sandbox_session_id=uuid.uuid4(),
        generation=1,
        image=settings.sandbox_image,
        runtime=settings.sandbox_runtime,
        # The deploy's real output budget, so a live run exercises the number the
        # lifecycle actually sends rather than the runner's fallback ceiling.
        output_bytes_cap=settings.sandbox_output_bytes_cap,
        # The SAME curated env the production lifecycle hands a session, so this run
        # inherits MPLBACKEND/HOME exactly as a chat's sandbox does — if that env
        # changes, this test follows it instead of drifting from it.
        env=SandboxSessionService._session_env(),
    )
    try:
        try:
            await runner.ensure_session(spec)
        except DependencyError as exc:
            # ``__cause__`` carries the runner's actual HTTP status; the wrapper text
            # alone ("sandbox runner unavailable") cannot distinguish a missing image
            # from a dead daemon, and that distinction is the whole point here.
            pytest.fail(
                f"sandbox-runner at {url!r} answered /health but could not start image "
                f"{settings.sandbox_image!r} ({exc}; cause: {exc.__cause__!r}). The runner "
                "drives the HOST Docker daemon, so the image must exist THERE — build it "
                f"with: {_BUILD_COMMAND}"
            )
        yield _LiveSandbox(runner=runner, spec=spec)
    finally:
        try:
            await runner.close_session(spec)
        except Exception as exc:  # noqa: BLE001 — teardown must not mask a real failure
            # Printed, not raised: an exception here would replace the test's own
            # verdict. Named so a leaked container is findable with `docker ps -a`.
            print(
                f"\nWARNING: could not destroy sandbox container "
                f"lumen-sandbox-{spec.sandbox_session_id}-{spec.generation} "
                f"({type(exc).__name__}) — remove it by hand."
            )


async def _run(sandbox: _LiveSandbox, code: str) -> RunResult:
    """Execute ``code`` in the live generation, shaped exactly as production does.

    ``execution_id`` doubles as the code-run id and as the run directory name, and
    ``LUMEN_OUTPUT_DIR`` is the same path ``SandboxService.execute`` passes — so the
    output-collection path under test is the real one, not a test-only arrangement.
    """
    execution_id = uuid.uuid4()
    return await sandbox.runner.execute(
        sandbox.spec,
        RunSpec(
            execution_id=execution_id,
            code=textwrap.dedent(code).strip() + "\n",
            env=(("LUMEN_OUTPUT_DIR", f"/workspace/.lumen/runs/{execution_id}/output"),),
        ),
    )


# --- (1) plain stdlib execution ----------------------------------------------


@_live
@pytest.mark.live
async def test_stdlib_code_really_executes_and_returns_its_stdout(
    sandbox: _LiveSandbox,
) -> None:
    """The claim nothing verified before: model-authored Python runs, and output returns.

    Asserts the *exact* stdout, not "something non-empty" — a run that silently
    produced nothing would otherwise still look green.
    """
    result = await _run(
        sandbox,
        """
        import math
        import sys

        print(math.factorial(5))
        print(sys.version_info.major)
        """,
    )

    assert result.status is CodeRunStatus.SUCCEEDED
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["120", "3"]
    # A clean run's stderr is empty. It is also where a matplotlib font-cache or
    # locale warning would surface, so an empty assertion is a real guard.
    assert result.stderr == ""
    assert result.duration_ms > 0
    # Reproducibility (E3-7): the runner reports the image it actually used. A
    # locally BUILT image has no registry RepoDigest, so this is the image id there
    # — non-empty is all that can honestly be asserted without a pushed image.
    assert result.image_digest


# --- (2) the real data-science stack (#503) -----------------------------------


@_live
@pytest.mark.live
async def test_pandas_and_numpy_do_real_work_in_the_execution_image(
    sandbox: _LiveSandbox,
) -> None:
    """#503, proven by running it: the image can do the data work it exists for.

    Before the fix ``SANDBOX_IMAGE`` named the runner's control-plane image, where
    every one of these imports raises ``ModuleNotFoundError``. The drift test reads
    ``requirements.txt``; this one imports the packages.
    """
    result = await _run(
        sandbox,
        """
        import numpy as np
        import pandas as pd

        frame = pd.DataFrame({"x": np.arange(1, 5), "y": np.arange(1, 5) ** 2})
        print(frame["y"].tolist())
        print(int(frame["y"].sum()))
        print(int(np.gcd(84, 132)))
        """,
    )

    assert result.status is CodeRunStatus.SUCCEEDED, result.stderr
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["[1, 4, 9, 16]", "30", "12"]
    assert result.stderr == ""


@_live
@pytest.mark.live
async def test_execution_image_is_not_the_runner_control_plane_image(
    sandbox: _LiveSandbox,
) -> None:
    """The #503 NEGATIVE, from inside the container: the control plane is not in here.

    ``fastapi`` and the Docker SDK are what the *runner's* image ships. Their absence
    is the positive proof that tenant code is no longer executing in the control-plane
    image — a distinction no amount of reading config can establish.
    """
    result = await _run(
        sandbox,
        """
        import importlib.util

        for name in ("fastapi", "docker", "pandas"):
            print(name, importlib.util.find_spec(name) is not None)
        """,
    )

    assert result.status is CodeRunStatus.SUCCEEDED, result.stderr
    assert result.stdout.splitlines() == [
        "fastapi False",
        "docker False",
        "pandas True",
    ]


#: Distributions the base ``python:*-slim`` image contributes on its own. They are not
#: part of the curated manifest and must not be asserted as drift — but the set is
#: deliberately tiny and explicit, so an image that ships anything ELSE unexpected
#: fails this test rather than passing under a wildcard.
_BASE_IMAGE_DISTRIBUTIONS = frozenset({"pip", "setuptools", "wheel"})


def _canonical(name: str) -> str:
    """PEP-503 name normalisation — ``et_xmlfile`` and ``et-xmlfile`` are one name."""
    return re.sub(r"[-_.]+", "-", name).lower()


@_live
@pytest.mark.live
async def test_the_image_on_the_host_really_ships_the_configured_manifest(
    sandbox: _LiveSandbox,
) -> None:
    """The axis the offline drift guard cannot reach: what is actually INSTALLED.

    ``tests/test_sandbox_exec_image.py`` proves that
    ``Settings.sandbox_preinstalled_packages`` equals ``sandbox_exec/requirements.txt``
    — two files in this repository. Neither of them is the image. ``SANDBOX_IMAGE`` is
    a **mutable tag**, so a stale build, a retag, or a hand-built image passes that
    guard while package admission cheerfully skips the install for a distribution that
    is not in the container. The consequence is not a policy hole but a broken
    capability: ``run_python(packages=["pandas"])`` is admitted with nothing to
    install, and the run dies on ``ModuleNotFoundError``.

    So this asserts the pair that matters — every configured pin present at **exactly**
    the configured version, inside the container that a chat turn would run in — and
    that the image ships nothing beyond the manifest except the base image's own
    packaging tools.
    """
    result = await _run(
        sandbox,
        """
        import importlib.metadata as metadata

        for distribution in metadata.distributions():
            print(f"{distribution.metadata['Name']}=={distribution.version}")
        """,
    )

    assert result.status is CodeRunStatus.SUCCEEDED, result.stderr
    installed = {
        _canonical(line.split("==", 1)[0]): line.split("==", 1)[1]
        for line in result.stdout.splitlines()
        if "==" in line
    }
    # A custom manifest may legitimately carry a bare name: admission then treats any
    # installed version as satisfying it, so presence is all that can be asserted for
    # one. The shipped manifest is fully pinned (the offline guard requires ``==``).
    configured = {
        _canonical(entry.split("==", 1)[0]): (entry.split("==", 1)[1] if "==" in entry else None)
        for entry in Settings().sandbox_preinstalled_packages
    }
    assert configured, "the configured manifest is empty — nothing would be asserted"

    missing = {name: version for name, version in configured.items() if name not in installed}
    wrong = {
        name: (version, installed[name])
        for name, version in configured.items()
        if version is not None and name in installed and installed[name] != version
    }
    extra = set(installed) - set(configured) - _BASE_IMAGE_DISTRIBUTIONS

    assert not missing, (
        "the running execution image does not contain distributions the admission path "
        f"treats as pre-installed (so their requests are admitted with no install and "
        f"then fail at import): {sorted(missing)} — rebuild it ({_BUILD_COMMAND}) and, "
        "outside local dev, pin SANDBOX_IMAGE by digest so a tag cannot drift"
    )
    assert not wrong, f"configured version != installed version (configured, installed): {wrong}"
    assert not extra, (
        f"the image ships distributions the manifest does not declare: {sorted(extra)} — "
        "sandbox_exec/requirements.txt is meant to be the exhaustive closure"
    )


# --- (3) headless matplotlib --------------------------------------------------


@_live
@pytest.mark.live
async def test_matplotlib_renders_headless_and_the_png_is_collected(
    sandbox: _LiveSandbox,
) -> None:
    """Agg + a writable MPLCONFIGDIR, exercised rather than grepped.

    The container has no display and no X libraries, so a non-``Agg`` backend raises
    on ``import matplotlib.pyplot``; an unwritable ``MPLCONFIGDIR`` makes matplotlib
    fall back to a temp dir and print a warning that lands in the run's stderr. Both
    are asserted here, and the rendered PNG is followed all the way back out through
    the runner's output collection — the path that becomes a chat artifact.
    """
    result = await _run(
        sandbox,
        """
        import os

        import matplotlib
        import matplotlib.pyplot as plt

        print(matplotlib.get_backend().lower())
        figure, axes = plt.subplots()
        axes.plot([0, 1, 2], [0, 1, 4])
        target = os.path.join(os.environ["LUMEN_OUTPUT_DIR"], "chart.png")
        figure.savefig(target)
        print(os.path.getsize(target) > 0)
        """,
    )

    assert result.status is CodeRunStatus.SUCCEEDED, result.stderr
    assert result.stdout.splitlines() == ["agg", "True"]
    # No font-cache / config warning leaked into the run's captured output.
    assert result.stderr == ""

    collected = [value.filename for value in result.output_files]
    charts = [value for value in result.output_files if value.filename == "chart.png"]
    assert charts, f"the rendered PNG was not collected: {collected}"
    assert charts[0].content_type == "image/png"
    # The real PNG signature — proof of a rendered image, not an empty placeholder.
    assert charts[0].data.startswith(b"\x89PNG\r\n\x1a\n")


# --- (4) NEGATIVE: egress is off and fails closed -----------------------------


@_live
@pytest.mark.live
async def test_outbound_network_from_the_sandbox_fails_closed(
    sandbox: _LiveSandbox,
) -> None:
    """The NEGATIVE case: model code has no network route, and never gets one.

    ADR-0020 §2 creates every execution container with ``--network none``; ADR-0013
    §5 G2–G4 make that deny-by-default and put the cloud metadata IP permanently out
    of reach. This asserts the *observable* consequence in three directions — a raw
    IP (no route), a DNS name (no resolver), and the metadata IP (G4) — and fails
    loudly the moment any of them connects, which is the only way a silently-opened
    egress would ever be noticed.
    """
    result = await _run(
        sandbox,
        """
        import socket
        import sys

        targets = (("1.1.1.1", 53), ("pypi.org", 443), ("169.254.169.254", 80))
        for host, port in targets:
            try:
                socket.create_connection((host, port), timeout=5).close()
            except OSError as exc:
                print(f"denied {host}:{port} {type(exc).__name__}")
            else:
                print(f"REACHED {host}:{port}")
                sys.exit(1)
        print("all outbound attempts failed closed")
        """,
    )

    assert result.status is CodeRunStatus.SUCCEEDED, result.stderr
    assert "REACHED" not in result.stdout, (
        "the sandbox reached the network — egress must be deny-by-default "
        f"(ADR-0013 §5 G2–G4):\n{result.stdout}"
    )
    lines = result.stdout.splitlines()
    assert sum(1 for line in lines if line.startswith("denied ")) == 3
    assert lines[-1] == "all outbound attempts failed closed"


@_live
@pytest.mark.live
async def test_failing_code_surfaces_a_real_nonzero_exit_and_its_traceback(
    sandbox: _LiveSandbox,
) -> None:
    """A NEGATIVE for the result mapping: a crash is reported as one, with its reason.

    A runner that swallowed the exit code (or mapped every outcome to ``succeeded``)
    would make every assertion above vacuous, so the failing direction is pinned too.
    """
    result = await _run(sandbox, "raise ValueError('boom-42')")

    assert result.status is CodeRunStatus.FAILED
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "ValueError: boom-42" in result.stderr


# --- ADR-0020 §4: one generation is one reusable environment ------------------


@_live
@pytest.mark.live
async def test_one_generation_is_a_reusable_environment_across_executions(
    sandbox: _LiveSandbox,
) -> None:
    """The whole point of ADR-0020: a chat's sandbox is a *session*, not a one-shot.

    Two executions in one generation must share the workspace and the interpreter's
    filesystem, which is what makes "now plot the dataframe from before" possible. No
    offline test can show this — a fake runner shares state trivially.
    """
    first = await _run(
        sandbox,
        """
        from pathlib import Path

        Path("/workspace/carried.txt").write_text("42", encoding="utf-8")
        print("wrote")
        """,
    )
    second = await _run(
        sandbox,
        """
        from pathlib import Path

        print(Path("/workspace/carried.txt").read_text(encoding="utf-8"))
        """,
    )

    assert first.status is CodeRunStatus.SUCCEEDED, first.stderr
    assert second.status is CodeRunStatus.SUCCEEDED, second.stderr
    assert second.stdout.strip() == "42"
