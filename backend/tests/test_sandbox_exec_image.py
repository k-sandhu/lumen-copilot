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


# --- #504: an honest package story --------------------------------------------


def test_preinstalled_setting_matches_the_image_manifest_exactly() -> None:
    """The drift guard: admin/config truth and image contents cannot diverge silently.

    ``SANDBOX_PREINSTALLED_PACKAGES`` is what the admission path treats as "already
    in the image, no install needed". If the image gains or drops a distribution
    without this list following, a request either fails at import time or is
    refused for a package that is right there — so the two are asserted equal.
    """
    assert sandbox_settings().sandbox_preinstalled_packages == _manifest()


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
