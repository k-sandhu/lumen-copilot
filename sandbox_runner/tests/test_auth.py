"""The internal runner API authenticates every capability endpoint (#508).

The runner is the single holder of the Docker socket, and socket authority is
host-root-equivalent. Before this, an SSRF in the backend or any compromised
container on the compose network could `PUT /sessions/<uuid>` and have it execute
arbitrary code. These pin the two halves: nothing runs without the shared secret,
and the process refuses to start without one configured.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from lumen_sandbox_runner.auth import (
    TOKEN_ENV,
    TOKEN_HEADER,
    RunnerTokenNotConfigured,
    configured_token,
)
from lumen_sandbox_runner.main import app

_TOKEN = "t" * 48

#: One entry per capability endpoint. Parametrised rather than spot-checked because
#: the failure mode being fixed is "a route that forgot its dependency" — a test that
#: covers only the routes someone remembered proves nothing about the next one.
_ENDPOINTS = [
    ("PUT", "/sessions/00000000-0000-0000-0000-000000000001"),
    ("GET", "/sessions/00000000-0000-0000-0000-000000000001"),
    ("POST", "/sessions/00000000-0000-0000-0000-000000000001/executions"),
    ("DELETE", "/sessions/00000000-0000-0000-0000-000000000001"),
    ("POST", "/sessions/00000000-0000-0000-0000-000000000001/cancel"),
]


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, _TOKEN)


@pytest.mark.parametrize(("method", "path"), _ENDPOINTS)
async def test_no_token_is_401_and_nothing_runs(method: str, path: str) -> None:
    """Deny by default: an unauthenticated caller never reaches the engine.

    Asserted as 401 specifically, not merely "an error": a 422 would mean the request
    body was parsed and the route was entered, which is the engine's doorstep.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path, json={})

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), _ENDPOINTS)
async def test_a_wrong_token_is_401(method: str, path: str) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path, json={}, headers={TOKEN_HEADER: "w" * 48})

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), _ENDPOINTS)
async def test_the_right_token_gets_past_auth(method: str, path: str) -> None:
    """The control: these tests must fail for the RIGHT reason.

    A correctly-authenticated call gets past the gate and is then judged on its
    merits — here an empty body, so 422 (or 200 for the no-body DELETE). Without
    this, `require_token` could reject everything and the tests above would still
    pass while the runner was simply broken.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path, json={}, headers={TOKEN_HEADER: _TOKEN})

    assert response.status_code != 401


async def test_health_stays_open_so_liveness_does_not_depend_on_auth() -> None:
    """A deliberate exception, pinned so it reads as a decision rather than a gap.

    `/health` returns a constant and grants no capability, and a 401 from any other
    route already reveals that the runner exists. Gating it would only make a
    mistyped token look like "the runner is down" instead of "the runner is
    misconfigured".
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_an_unset_token_refuses_to_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast, never fall back to no auth — that is the state being fixed."""
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(RunnerTokenNotConfigured):
        configured_token()


@pytest.mark.parametrize("value", ["", "   ", "short"])
def test_a_blank_or_weak_token_refuses_to_serve(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A blank value must not read as "configured", and a guessable one is worse
    than none: it looks like protection while providing little."""
    monkeypatch.setenv(TOKEN_ENV, value)
    with pytest.raises(RunnerTokenNotConfigured):
        configured_token()
