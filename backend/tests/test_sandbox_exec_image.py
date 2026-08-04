"""The sandbox EXECUTION image and its package story (issues #503, #504).

Two defects found while diagnosing "``run_python`` never worked":

* **#503** — ``SANDBOX_IMAGE`` defaulted to ``lumen-sandbox-runner:0.2.0``, the
  runner's own CONTROL-PLANE image (python-slim + fastapi/docker/pydantic). Model
  code therefore ran with **none** of the data-science stack ADR-0013 §3 specifies,
  so "run Python" could not do data work at all. A dedicated execution image
  (``sandbox_exec/``) now ships that stack, and the default points at it.
* **#504** — with ``allowed_packages`` empty (the deny-by-default state of every
  tenant) and no sandbox egress, EVERY ``packages=[...]`` request was refused and
  there was no install path. Distributions the image already ships need no install
  at all, so they are admitted without one; anything else still needs the admin
  allow-list AND the runner's outbound network.

These tests are the drift guard: config, the image manifest, the Dockerfile and
compose must keep agreeing, because nothing else notices when they stop.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.core.config import _DEFAULT_SANDBOX_PREINSTALLED_PACKAGES, Settings
from app.sandbox.service import PackagePolicyError, validate_requested_packages
from tests._sandbox_helpers import sandbox_settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXEC_DIR = _REPO_ROOT / "sandbox_exec"
_REQUIREMENTS = _EXEC_DIR / "requirements.txt"
_DOCKERFILE = _EXEC_DIR / "Dockerfile"
_COMPOSE = _REPO_ROOT / "docker-compose.yml"

#: The runner's control-plane image — what #503 wrongly executed tenant code in.
_RUNNER_IMAGE_PREFIX = "lumen-sandbox-runner"


def _manifest() -> tuple[str, ...]:
    """The execution image's package manifest — one ``name==version`` pin per line."""
    lines = (
        line.split("#", 1)[0].strip()
        for line in _REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    )
    return tuple(line for line in lines if line)


def _dockerfile() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


def _compose_service_for(image: str) -> dict[str, object]:
    compose = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]
    matches = [value for value in services.values() if value.get("image") == image]
    assert matches, f"no compose service builds {image}"
    return matches[0]


# --- #503: the execution image is a REAL execution image ----------------------


def test_default_sandbox_image_is_not_the_runner_control_plane_image() -> None:
    """The negative test for #503: tenant code must not run in the runner's image.

    The control-plane image carries fastapi/docker/pydantic and NO scientific
    stack; executing model code there is both the wrong capability and a needless
    widening of what a contained escape would find.
    """
    image = sandbox_settings().sandbox_image

    assert not image.startswith(_RUNNER_IMAGE_PREFIX)
    assert image.startswith("lumen-sandbox-exec:")


def test_compose_builds_the_default_execution_image_under_the_sandbox_profile() -> None:
    """The image the app names must be one the documented build actually produces.

    The runner launches it on the HOST daemon, so an image nobody builds is a run
    that fails at container creation with no policy explanation.
    """
    service = _compose_service_for(sandbox_settings().sandbox_image)

    assert service["build"]["context"] == "./sandbox_exec"
    assert "sandbox" in service["profiles"]


def test_execution_image_ships_the_curated_scientific_stack() -> None:
    """ADR-0013 §3: pandas, numpy, matplotlib (+ openpyxl), every one version-pinned."""
    names = {entry.split("==")[0].lower().replace("_", "-") for entry in _manifest()}

    assert {"numpy", "pandas", "matplotlib", "openpyxl"} <= names
    assert all("==" in entry for entry in _manifest())


def test_execution_image_base_is_digest_pinned_and_never_latest() -> None:
    """No ``:latest`` (root AGENTS.md §10); ADR-0013 §3 asks for a digest pin.

    Scope, stated so it is not mistaken for more than it is: this asserts the BUILD
    input — the ``FROM`` line's base layer. The reference the runner actually
    launches is ``SANDBOX_IMAGE``, pinned at boot by ``Settings`` and covered by
    ``tests/test_sandbox_config.py``. Both are needed; neither implies the other.
    """
    from_lines = [
        line for line in _dockerfile().splitlines() if line.strip().upper().startswith("FROM ")
    ]

    assert from_lines
    for line in from_lines:
        assert ":latest" not in line
        assert "@sha256:" in line


def test_execution_image_makes_matplotlib_work_headless() -> None:
    """Headless matplotlib in a capped, offline container is the real trap.

    Without an ``Agg`` backend there is no display to draw on, and without a
    WRITABLE ``MPLCONFIGDIR`` the first ``import matplotlib.pyplot`` tries to build
    a font cache in ``$HOME`` and can fail (or hang) inside the sandbox layout. The
    image sets both and warms the cache at BUILD time so no run pays for it.
    """
    body = _dockerfile()

    assert "MPLBACKEND=Agg" in body
    assert "MPLCONFIGDIR=" in body
    assert "font_manager" in body  # the build-time cache warm-up


def test_execution_image_defaults_to_an_unprivileged_user() -> None:
    """The image's own default uid must stay non-root (#506 review).

    ADR-0020 deliberately overrides this at launch with ``--user 0:0`` — schema-pinned
    as ``Literal["0:0"]`` on the wire — so this assertion is NOT about what a governed
    run gets. It is about the fallback: the image's default is what a hand-run smoke
    test, a future non-Docker launcher, or a CI job would use, and a refactor that
    dropped ``USER`` would make the image root-by-default with no diff-time signal.
    Both halves of the posture are asserted here so they cannot drift apart silently.
    """
    body = _dockerfile()

    assert "\nUSER sandbox" in body, (
        "sandbox_exec/Dockerfile must end with an unprivileged USER — the runner's "
        "--user 0:0 override is the governed path, not the image's default"
    )
    # The comment explaining WHY root overrides it must travel with the line, so the
    # next reader does not "fix" the apparent contradiction by deleting one of them.
    assert "ADR-0020" in body


# --- #504: an honest package story --------------------------------------------


def test_preinstalled_setting_matches_the_image_manifest_exactly() -> None:
    """The drift guard: admin/config truth and image contents cannot diverge silently.

    ``SANDBOX_PREINSTALLED_PACKAGES`` is what the admission path treats as "already
    in the image, no install needed". If the image gains or drops a distribution
    without this list following, a request either fails at import time or is
    refused for a package that is right there — so the two are asserted equal.

    Asserted against the **shipped constant**, not ``Settings``. Reading it through
    ``sandbox_settings()`` meant reading the ambient environment (and ``.env``), so an
    operator override on the machine running the suite silently replaced the value
    under test: the guard would then confirm that the *ambient* value matched the
    manifest and say nothing about what the repository ships. The constant is the
    only value a checkout can be held to.

    **Scope, so this is not mistaken for more than it is.** Both sides here are files
    in this repository — the guard proves config and the build input agree, never that
    the image ON THE HOST contains them. ``SANDBOX_IMAGE`` is a mutable tag, so a
    stale or retagged image passes this happily while package admission skips installs
    for distributions that are not there. That axis needs a real container and is
    asserted in ``tests/test_sandbox_execution_live.py``.
    """
    assert _DEFAULT_SANDBOX_PREINSTALLED_PACKAGES == _manifest()
    # The field's default is the constant, so config cannot drift from it either.
    assert Settings.model_fields["sandbox_preinstalled_packages"].default == _manifest()


def test_prebaked_package_is_admitted_with_no_install_on_an_empty_allow_list() -> None:
    """#504's core: an empty allow-list must not refuse what the image already ships.

    The returned tuple is what the runner is asked to INSTALL — empty here, so the
    run never needs the wheel download that an offline deploy cannot perform.
    """
    preinstalled = sandbox_settings().sandbox_preinstalled_packages

    assert (
        validate_requested_packages(
            ("pandas", "NumPy"), allowed=(), denied=(), preinstalled=preinstalled
        )
        == ()
    )


def test_prebaked_package_with_a_satisfied_specifier_needs_no_install() -> None:
    """A pin the shipped version satisfies is satisfied — by the image, offline."""
    preinstalled = ("pandas==3.0.5",)

    assert (
        validate_requested_packages(
            ("pandas>=2",), allowed=(), denied=(), preinstalled=preinstalled
        )
        == ()
    )


def test_unknown_package_is_refused_with_an_actionable_reason() -> None:
    """The negative test: not in the image AND not allow-listed ⇒ a refusal that says so."""
    with pytest.raises(PackagePolicyError, match="scikit-learn") as refusal:
        validate_requested_packages(
            ("scikit-learn",),
            allowed=(),
            denied=(),
            preinstalled=sandbox_settings().sandbox_preinstalled_packages,
        )

    detail = refusal.value.detail or ""
    assert "not installed in the code sandbox image" in detail
    assert "allowed-packages" in detail


def test_prebaked_package_is_still_refused_when_the_admin_denies_it() -> None:
    """Deny wins over the image: an explicit denial is an admin decision, not a gap."""
    with pytest.raises(PackagePolicyError, match="pandas"):
        validate_requested_packages(
            ("pandas",),
            allowed=("*",),
            denied=("pandas",),
            preinstalled=("pandas==3.0.5",),
        )


def test_version_the_image_does_not_ship_needs_the_allow_list() -> None:
    """A pin the image cannot satisfy is a real install — so it needs a real grant.

    Refusing it silently as "pandas is available" would run the WRONG version; the
    refusal names the version that is actually there.
    """
    with pytest.raises(PackagePolicyError, match="3.0.5") as refusal:
        validate_requested_packages(
            ("pandas==1.5.3",), allowed=(), denied=(), preinstalled=("pandas==3.0.5",)
        )

    assert "allowed-packages" in (refusal.value.detail or "")
    assert validate_requested_packages(
        ("pandas==1.5.3",), allowed=("pandas",), denied=(), preinstalled=("pandas==3.0.5",)
    ) == ("pandas==1.5.3",)


def test_extras_still_need_a_grant_because_the_image_ships_only_the_base() -> None:
    """``pandas[excel]`` pulls extra distributions the manifest does not promise."""
    with pytest.raises(PackagePolicyError, match="pandas"):
        validate_requested_packages(
            ("pandas[plot]",), allowed=(), denied=(), preinstalled=("pandas==3.0.5",)
        )


# --- #508: the runner's network boundary, asserted rather than described --------


def _compose() -> dict:
    import yaml

    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def _networks(service: dict) -> set[str]:
    nets = service.get("networks") or []
    return set(nets.keys()) if isinstance(nets, dict) else set(nets)


def test_only_the_api_and_worker_share_the_runner_network() -> None:
    """The second control behind the shared secret (#508, ADR-0020 §2).

    The runner holds the Docker socket. Authentication answers "who is calling"; this
    answers "who can call at all". Before this, every service in the project sat on
    one default network, so `postgres`, `searxng` or the frontend could open a socket
    to the Docker-socket holder — and a compromise of any of them was a compromise of
    code execution.

    Asserted from the compose file because a network boundary is not a property of any
    Python module: nothing else in the suite notices when someone adds a service to
    this network, and the harm of that is silent.
    """
    compose = _compose()
    services = compose["services"]

    assert "sandbox-net" in compose.get("networks", {}), "the dedicated network is gone"

    on_net = {name for name, svc in services.items() if "sandbox-net" in _networks(svc)}
    assert on_net == {"backend", "worker", "sandbox-runner"}, (
        "exactly the API, the worker and the runner may share the runner's network; "
        f"found {sorted(on_net)}"
    )

    # And the runner joins NOTHING else — being on `default` too would undo it.
    assert _networks(services["sandbox-runner"]) == {"sandbox-net"}


def test_the_runner_token_is_never_a_literal_in_the_committed_compose_file() -> None:
    """A secret in a committed file is not a secret (#508).

    Every service that needs it interpolates from the host shell or `.env`, both
    uncommitted. Asserted on the RAW text rather than the parsed value because
    interpolation is exactly what parsing would resolve away.
    """
    raw = _COMPOSE.read_text(encoding="utf-8")

    for line in raw.splitlines():
        if "SANDBOX_RUNNER_TOKEN:" in line:
            value = line.split("SANDBOX_RUNNER_TOKEN:", 1)[1].strip()
            assert value.startswith("${"), f"a literal token is committed: {line.strip()!r}"

    # The runner and both callers must actually carry it, or the stack 401s at runtime.
    services = _compose()["services"]
    for name in ("backend", "worker", "sandbox-runner"):
        assert "SANDBOX_RUNNER_TOKEN" in services[name].get(
            "environment", {}
        ), f"{name} does not receive the runner token"
