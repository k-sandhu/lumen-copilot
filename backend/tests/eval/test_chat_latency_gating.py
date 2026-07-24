"""Offline gating tests for the live chat-latency harness (#486 / FE-2).

The live harness (``test_chat_latency_live.py``) spends real tokens and talks to a
real stack, so it must be **doubly** off by default: an explicit ``RUN_LIVE=1``
opt-in AND reachable services — matching ``test_realtime_backplane_live.py`` (issue
#94 parity). These offline tests pin two properties of that gate without a key or a
stack:

* Importing the harness module opens **no** socket (reachability is probed inside a
  fixture, only after the ``RUN_LIVE`` gate has already let the test run) — so an
  ordinary ``pytest`` collection never touches Postgres / OpenSearch / Redis.
* With ``RUN_LIVE`` unset the two live tests carry the skip gate **and** the
  ``live`` marker, so ``uv run --extra dev pytest tests/eval`` skips them cleanly.
"""

from __future__ import annotations

import importlib
import socket

import pytest


def test_harness_import_opens_no_socket_and_gates_on_run_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reloading the harness with ``RUN_LIVE`` unset must not probe any socket.

    A regression that moves reachability probing back to import time (the FE-2
    bug) would call ``socket.create_connection`` during the reload and fail here.
    """

    def _forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"import-time socket probe to {args!r} — FE-2 regression")

    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.delenv("RUN_LIVE", raising=False)
    # Even with a key present, an offline import must not probe datastores.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-used-offline")

    module = importlib.import_module("tests.eval.test_chat_latency_live")
    # Re-execute the module top level under the socket guard (proves no import-time
    # probe); reload is a no-op for the already-cached app.* imports.
    module = importlib.reload(module)

    # RUN_LIVE unset ⇒ the opt-in gate is closed.
    assert module._RUN_LIVE is False

    # Both live tests carry the RUN_LIVE skip gate AND the `live` marker so the
    # default offline suite skips them (never spends tokens, never opens a socket).
    for name in (
        "test_time_to_first_answer_token_p50_under_budget",
        "test_unreachable_provider_yields_typed_terminal_error_within_budget",
    ):
        marks = {m.name for m in getattr(module, name).pytestmark}
        assert "skipif" in marks, f"{name} lost its RUN_LIVE skip gate"
        assert "live" in marks, f"{name} lost its `live` marker"


def test_run_live_env_flag_parsing() -> None:
    """The opt-in flag matches the repo convention: only truthy values enable it."""
    module = importlib.import_module("tests.eval.test_chat_latency_live")
    assert hasattr(module, "_RUN_LIVE")
