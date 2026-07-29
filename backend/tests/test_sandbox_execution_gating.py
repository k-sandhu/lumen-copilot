"""Offline gating guards for the live sandbox-execution test (issue #506).

``test_sandbox_execution_live.py`` creates and destroys real Docker containers, so
it must be **doubly** off by default — an explicit ``RUN_LIVE=1`` opt-in AND a
reachable ``sandbox-runner`` — matching ``test_realtime_backplane_live.py`` and
``tests/eval/test_chat_latency_live.py`` (issue #94 parity, R2-5). These tests need
neither a runner nor Docker; they pin the gate itself:

* importing the live module opens **no** socket (reachability is probed inside a
  fixture, only after the ``RUN_LIVE`` gate has let a test through), so an ordinary
  ``pytest`` collection never touches the sandbox stack;
* the opt-in is a **strict** ``== "1"``. A lenient truthy parse was a real defect on
  the token-spending harness, where ``"no"`` and ``"true"`` both opened the gate;
* **every** ``test_*`` in the live module carries the skip gate and the ``live``
  marker. The list is discovered, not hardcoded, so a newly added live test cannot
  quietly escape the gate;
* an opted-in run with no runner **skips with a reason that names what is missing**
  — the failure mode that let #503 stay dark was a suite that looked fine while
  proving nothing, and a silent skip is that same failure wearing a green tick.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import socket
from types import ModuleType
from typing import Any

import pytest

_LIVE_MODULE = "tests.test_sandbox_execution_live"


def _live_tests(module: ModuleType) -> dict[str, Any]:
    """Every module-level test function in the live module, by name."""
    return {
        name: value
        for name, value in vars(module).items()
        if name.startswith("test_") and callable(value)
    }


def _fixture_body(fixture: Any) -> Any:
    """Recover a fixture's undecorated function across pytest's wrapping schemes.

    ``@pytest.fixture`` replaces the function with a guard that errors if it is
    called directly; the original hangs off ``__pytest_wrapped__`` (pytest 8.3, the
    pinned version) or ``_fixture_function`` (8.4+), with ``functools.wraps``'
    ``__wrapped__`` as the last resort.
    """
    wrapper = getattr(fixture, "__pytest_wrapped__", None)
    if wrapper is not None:
        return wrapper.obj
    inner = getattr(fixture, "_fixture_function", None)
    if inner is not None:
        return inner
    return getattr(fixture, "__wrapped__", fixture)


def _first_step(generator: Any) -> None:
    """Advance an async generator one step on a throwaway loop, surfacing its error."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(generator.__anext__())
    finally:
        loop.close()


def test_live_module_import_opens_no_socket_and_gates_on_run_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reloading the live module with ``RUN_LIVE`` unset must not probe anything.

    A regression that moved the reachability probe back to import time would resolve
    or connect during the reload and trip these guards — the same defect class the
    latency harness fixed as FE-2.
    """

    def _forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError(f"import-time network probe to {args!r} — the gate leaked")

    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)
    monkeypatch.delenv("RUN_LIVE", raising=False)

    module = importlib.reload(importlib.import_module(_LIVE_MODULE))

    assert module._RUN_LIVE is False

    tests = _live_tests(module)
    # A silently-empty discovery would make every assertion below vacuous.
    assert len(tests) >= 4, f"expected the live coverage to still be there, found {sorted(tests)}"
    for name in tests:
        marks = {mark.name for mark in getattr(module, name).pytestmark}
        assert "skipif" in marks, f"{name} lost its RUN_LIVE skip gate"
        assert "live" in marks, f"{name} lost its `live` marker"


@pytest.mark.parametrize("value", ["no", "true", "True", "0", "2", "yes", "", None])
def test_run_live_gate_is_strict_and_stays_off_for_non_1(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """Only the exact string ``"1"`` may launch containers on someone's machine.

    Every other value — including a bare unset — must leave the gate closed, so each
    live test keeps a skip condition that is actually ``True`` (a mark that is merely
    *present* but evaluates False would still run).
    """
    if value is None:
        monkeypatch.delenv("RUN_LIVE", raising=False)
    else:
        monkeypatch.setenv("RUN_LIVE", value)

    module = importlib.reload(importlib.import_module(_LIVE_MODULE))
    try:
        assert module._RUN_LIVE is False, f"RUN_LIVE={value!r} must NOT enable the live test"
        for name in _live_tests(module):
            marks = list(getattr(module, name).pytestmark)
            assert any(mark.name == "live" for mark in marks), f"{name} lost its `live` marker"
            skipif = next((mark for mark in marks if mark.name == "skipif"), None)
            assert skipif is not None, f"{name} lost its RUN_LIVE skip gate under {value!r}"
            assert skipif.args[0] is True, (
                f"RUN_LIVE={value!r} left {name}'s skip condition False — it would RUN "
                "and start containers"
            )
    finally:
        # Leave the cached module in the OFF state so a later importer never inherits
        # a gate armed by this parametrization.
        monkeypatch.delenv("RUN_LIVE", raising=False)
        importlib.reload(module)


def test_run_live_gate_opens_only_on_exactly_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is not vacuously-off: ``RUN_LIVE=1`` (and only that) opens it.

    Without this, a gate hardwired to ``False`` would satisfy every test above while
    making the live coverage unreachable — dark in a new way.
    """
    monkeypatch.setenv("RUN_LIVE", "1")
    module = importlib.reload(importlib.import_module(_LIVE_MODULE))
    try:
        assert module._RUN_LIVE is True
        for name in _live_tests(module):
            skipif = next(
                mark for mark in getattr(module, name).pytestmark if mark.name == "skipif"
            )
            assert skipif.args[0] is False, f"{name} would still skip under RUN_LIVE=1"
    finally:
        monkeypatch.delenv("RUN_LIVE", raising=False)
        importlib.reload(module)


def test_opted_in_run_skips_loudly_when_the_runner_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opted in, but nothing to talk to ⇒ a NAMED skip, never a silent pass.

    Drives the live fixture's own body with the probe forced to fail, and asserts the
    skip reason names the URL, the reason it could not be reached, and the command
    that starts the service.
    """
    monkeypatch.setenv("RUN_LIVE", "1")
    module = importlib.reload(importlib.import_module(_LIVE_MODULE))
    try:

        async def _unreachable(url: str) -> None:
            raise OSError(f"no route to {url}")

        monkeypatch.setattr(module, "_probe", _unreachable)

        body = _fixture_body(module.sandbox)
        assert inspect.isasyncgenfunction(body), (
            "could not recover the live fixture's body — this guard would silently "
            f"stop checking anything (got {body!r})"
        )

        with pytest.raises(pytest.skip.Exception) as skipped:
            _first_step(body())

        reason = str(skipped.value)
        assert "sandbox-runner unreachable" in reason
        assert "OSError" in reason
        assert "docker compose --profile sandbox up -d sandbox-runner" in reason
    finally:
        monkeypatch.delenv("RUN_LIVE", raising=False)
        importlib.reload(module)
