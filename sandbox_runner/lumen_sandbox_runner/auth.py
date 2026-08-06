"""Shared-secret authentication for the internal runner API (#508).

The runner is the single holder of the Docker socket, and socket authority is
host-root-equivalent. Until now every endpoint was **unauthenticated**, so any
container that could reach it on the compose network — or an SSRF in the backend —
could issue::

    PUT  http://sandbox-runner:8000/sessions/<uuid>
    POST http://sandbox-runner:8000/sessions/<uuid>/executions

and have the socket holder execute arbitrary code. #507 removed the ability to make
it *pull* an attacker image (the reference is validated and resolved locally, and
the OCI runtime is taken from the runner's own configuration), but the command
channel itself was still open.

Two independent controls, because either alone is one mistake away from nothing:

* this token, which authenticates the CALLER, and
* a dedicated compose network, which limits who can open a socket at all.

``/health`` is deliberately **not** gated. It returns a constant, grants no
capability, and discloses nothing an unauthenticated 401 from any other route would
not — while gating it would make container liveness depend on auth configuration, so
a mistyped token would read as "the runner is down" instead of "the runner is
misconfigured". Liveness should answer whether the process is alive.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException, status

#: The header the backend/worker client sends. Not `Authorization`: this is a
#: service-to-service shared secret on an internal network, not a user credential,
#: and keeping it distinct means no proxy or library treats it as a bearer token to
#: be helpfully forwarded somewhere else.
TOKEN_HEADER = "X-Lumen-Runner-Token"

#: Where the secret comes from. Absent or blank is a hard startup failure, never a
#: silent fallback to "no auth" — that is exactly the state this fixes.
TOKEN_ENV = "SANDBOX_RUNNER_TOKEN"

#: Below this, a secret is guessable enough that having it is worse than not: it
#: reads as protection while providing little.
_MIN_TOKEN_LENGTH = 32


class RunnerTokenNotConfigured(RuntimeError):
    """Raised at STARTUP when no token is configured — the process must not serve."""


def configured_token() -> str:
    """The configured shared secret, or refuse to run.

    Fail-fast at startup rather than per-request: a runner that boots happily and
    then rejects everything is indistinguishable from a network fault, and one that
    boots and accepts everything is the bug being fixed.
    """
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise RunnerTokenNotConfigured(
            f"{TOKEN_ENV} must be set: the sandbox runner holds the Docker socket and "
            "refuses to serve an unauthenticated API"
        )
    if len(token) < _MIN_TOKEN_LENGTH:
        raise RunnerTokenNotConfigured(
            f"{TOKEN_ENV} must be at least {_MIN_TOKEN_LENGTH} characters"
        )
    return token


async def require_token(
    x_lumen_runner_token: str | None = Header(default=None, alias=TOKEN_HEADER),
) -> None:
    """Reject an unauthenticated or wrongly-authenticated request before any work.

    Declared as a router-level dependency so a new endpoint is authenticated by
    construction: forgetting to add it to a route is the failure mode that put this
    issue in the backlog, and a per-route decorator makes that failure silent.
    """
    expected = configured_token()
    # Constant-time: a naive `==` leaks the shared secret to anyone who can time the
    # response, and this caller is on the network by assumption.
    if x_lumen_runner_token is None or not secrets.compare_digest(x_lumen_runner_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="sandbox runner token missing or invalid",
        )
