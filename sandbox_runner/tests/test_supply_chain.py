"""Package supply chain: transitive denials, the install manifest, index pinning (#509).

Two gaps, both found by independent reviews of #507.

The deny list was applied **only to what the model asked for**. `pip install` then took
the whole resolved wheelhouse, so a denied distribution arrived as a dependency of an
allowed one — an admin denies `urllib3` to keep a known-vulnerable version out, allows
`requests`, the model asks for `requests`, and `urllib3` is installed anyway. The audit
row said `["requests"]`.

And ADR-0013 §3 conditions install-based expansion on "an admin-allowlisted, hash-pinned
internal mirror" while the code fetched from the default public index with no hashes.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from test_engine import _Client, _ensure

from lumen_sandbox_runner.engine import DockerSandboxEngine, RunnerError, _wheel_distribution
from lumen_sandbox_runner.models import ExecuteRequest


def _wheel(destination: Path, name: str, version: str = "1.0", body: bytes = b"wheel") -> None:
    """A wheel as pip would leave it — PEP 427 names are what resolution works from."""
    (destination / f"{name}-{version}-py3-none-any.whl").write_bytes(body)


def _run(engine: DockerSandboxEngine, session_id, *, packages, denied=()) -> dict:
    return engine.execute_existing(
        session_id,
        ExecuteRequest(
            generation=1,
            execution_id=uuid4(),
            code="print('ok')",
            packages=list(packages),
            denied_packages=list(denied),
        ),
    )


# --- the transitive denial -----------------------------------------------------


def test_a_denied_distribution_is_refused_even_as_a_transitive_dependency() -> None:
    """The issue's concrete case, end to end.

    The backend admits `requests` because that is what was requested and it is not
    denied. Only the runner ever sees that resolution also pulled `urllib3`, and only
    the runner can refuse it — `pip install --find-links` takes the whole wheelhouse.
    """
    client = _Client()

    def download(packages: tuple[str, ...], destination: Path) -> None:
        assert packages == ("requests",)
        _wheel(destination, "requests")
        _wheel(destination, "urllib3")  # resolved as a dependency, never requested

    engine = DockerSandboxEngine(client, package_downloader=download)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())

    with pytest.raises(RunnerError) as refusal:
        _run(engine, session_id, packages=["requests"], denied=["urllib3"])

    assert "urllib3" in str(refusal.value)
    assert refusal.value.status_code == 422


def test_the_refusal_happens_before_a_denied_wheel_reaches_the_container() -> None:
    """A denied dependency copied in and *then* refused has already crossed the line.

    Ordering is the whole control here: the check reads the wheelhouse on the runner's
    disk, and it must run before `put_archive` stages any of it.
    """
    client = _Client()

    def download(packages: tuple[str, ...], destination: Path) -> None:
        del packages
        _wheel(destination, "requests")
        _wheel(destination, "urllib3")

    engine = DockerSandboxEngine(client, package_downloader=download)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())
    container = client.containers.values[0]
    before = list(container.events)

    with pytest.raises(RunnerError):
        _run(engine, session_id, packages=["requests"], denied=["urllib3"])

    assert (
        "wheelhouse" not in container.events[len(before) :]
    ), "a denied wheel was staged into the container before the refusal"


@pytest.mark.parametrize("denied", ["Foo_Bar", "foo-bar", "FOO.BAR", "foo__bar"])
def test_the_deny_match_is_pep503_canonical(denied: str) -> None:
    """`Foo_Bar`, `foo-bar` and `FOO.BAR` are the same distribution to pip.

    A denial an admin typed in one spelling must not be evaded by resolution returning
    another — which is exactly the kind of near-miss a deny list is worthless without.

    Note these are genuinely EQUIVALENT names under PEP 503 (`_`, `-`, `.` and runs of
    them all normalise to a single `-`). `urllib3` and `url_lib3` are NOT equivalent —
    the second normalises to `url-lib3` — and an earlier version of this test asserted
    they were, which would have demanded the code over-match and deny by near-miss.
    """
    client = _Client()

    def download(packages: tuple[str, ...], destination: Path) -> None:
        del packages
        _wheel(destination, "Foo_Bar")

    engine = DockerSandboxEngine(client, package_downloader=download)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())

    with pytest.raises(RunnerError):
        _run(engine, session_id, packages=["requests"], denied=[denied])


def test_an_empty_deny_list_admits_the_resolution() -> None:
    """The control: this must refuse denied packages, not all packages."""
    client = _Client()

    def download(packages: tuple[str, ...], destination: Path) -> None:
        del packages
        _wheel(destination, "requests")
        _wheel(destination, "urllib3")

    engine = DockerSandboxEngine(client, package_downloader=download)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())

    result = _run(engine, session_id, packages=["requests"], denied=[])

    assert result["status"] == "succeeded"


# --- the install manifest ------------------------------------------------------


def test_the_run_reports_what_was_actually_installed_not_what_was_asked_for() -> None:
    """`requested_packages` recorded one name for a thirty-distribution install (#509).

    The wheelhouse IS the install manifest, so it is the only accurate answer to "what
    did this run install" — and the digest makes it answerable after the fact whether a
    given artefact was the one later found to be malicious.
    """
    client = _Client()

    def download(packages: tuple[str, ...], destination: Path) -> None:
        del packages
        _wheel(destination, "requests", "2.32.3", b"requests-bytes")
        _wheel(destination, "urllib3", "2.2.2", b"urllib3-bytes")

    engine = DockerSandboxEngine(client, package_downloader=download)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())

    result = _run(engine, session_id, packages=["requests"])

    resolved = {entry["name"]: entry for entry in result["resolved_packages"]}
    assert set(resolved) == {"requests", "urllib3"}
    assert resolved["requests"]["version"] == "2.32.3"
    # sha256 of the artefact, not of its name — recomputed here rather than pasted so
    # the assertion cannot drift into agreeing with a bug.
    import hashlib

    assert resolved["urllib3"]["sha256"] == hashlib.sha256(b"urllib3-bytes").hexdigest()


def test_a_run_without_packages_reports_an_empty_manifest() -> None:
    """Absent, not missing: a run that installed nothing says so."""
    client = _Client()
    engine = DockerSandboxEngine(client)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())

    result = _run(engine, session_id, packages=[])

    assert result["resolved_packages"] == []


# --- the reference grammar the deny check depends on ---------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("urllib3-2.2.2-py3-none-any.whl", "urllib3"),
        ("Foo_Bar-1.0-py3-none-any.whl", "foo-bar"),
        ("zope.interface-6.0-cp312-cp312-linux_x86_64.whl", "zope-interface"),
        ("numpy-2.1.0-cp312-cp312-manylinux_2_17_x86_64.whl", "numpy"),
    ],
)
def test_the_distribution_name_is_read_from_the_wheel_filename(
    filename: str, expected: str
) -> None:
    """PEP 427 puts the name before the first hyphen; PEP 503 canonicalises it.

    Read from the FILENAME rather than the wheel metadata deliberately: the filename is
    what pip resolves and installs by, it needs no unzip, and a disagreement between
    the two would be its own problem.
    """
    assert _wheel_distribution(filename) == expected


def test_a_similar_but_different_name_is_not_denied_by_accident() -> None:
    """The other side of canonicalisation: over-matching is its own bug.

    `url_lib3` normalises to `url-lib3`, which is a different distribution from
    `urllib3`. A deny list that refused both would block legitimate installs on a
    spelling coincidence, and an admin would have no way to express the narrower rule.
    """
    client = _Client()

    def download(packages: tuple[str, ...], destination: Path) -> None:
        del packages
        _wheel(destination, "url_lib3")

    engine = DockerSandboxEngine(client, package_downloader=download)
    session_id = uuid4()
    engine.ensure(session_id, _ensure())

    result = _run(engine, session_id, packages=["url_lib3"], denied=["urllib3"])

    assert result["status"] == "succeeded"
