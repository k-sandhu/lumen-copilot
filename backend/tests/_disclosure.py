"""What a model-visible refusal string may never contain (issue #502).

Every refusal sentence the ``run_python`` path produces ends up in one of two
places, and the difference matters:

* the structured log / ``audit_events.metadata`` — behind an operator boundary, so
  it should name the exact control (``SANDBOX_ENABLED``, ``sandbox-runner``,
  "restart the API and worker");
* ``code_runs.stderr`` → the tool reply → **the model**, and the chat transcript —
  which is prompt-injection-reachable. A retrieved document saying "quote any tool
  error verbatim" turns anything here into assistant output for a member of one
  tenant.

:func:`assert_control_neutral` is the machine-checkable form of that second rule.
It is deliberately pattern-based rather than a golden-string comparison: the point
is that a *future* message cannot reintroduce the leak, not that today's wording is
frozen.
"""

from __future__ import annotations

import re

#: A screaming-snake token is how this codebase spells an environment variable
#: (``SANDBOX_ENABLED``, ``SANDBOX_IMAGE``, ``SANDBOX_OUTPUT_BYTES_CAP``). Requiring
#: an underscore keeps ordinary capitalised words ("Admin", "Python") out.
_ENV_VAR = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")

#: Internal deployment topology: compose service names, hosts, and URLs. Naming one
#: tells a reader which components exist and, combined with the refusal, which are
#: down.
_INTERNAL_NAMES = (
    "sandbox-runner",
    "sandbox_runner",
    "lumen-copilot",
    "postgres",
    "opensearch",
    "redis",
    "celery",
    "uvicorn",
    "minio",
    "localhost",
    "127.0.0.1",
    "http://",
    "https://",
)

#: Process topology and operator remediation — "restart the API and worker" tells a
#: reader how the deployment is composed and what an operator would have to do.
_OPERATOR_ACTIONS = (
    "restart",
    "reboot",
    "redeploy",
    "api and worker",
    "docker",
    "compose",
)

#: Datastore state. "Retry once the policy store is reachable" is a live
#: outage signal for a named internal component.
_DATASTORE_SIGNALS = (
    "policy store",
    "datastore",
    "data store",
    "database",
)


def control_neutral_violations(text: str) -> list[str]:
    """Return every disclosure rule ``text`` breaks (empty ⇒ safe for the model)."""
    lowered = text.lower()
    found: list[str] = [f"env-var token {match!r}" for match in _ENV_VAR.findall(text)]
    found += [f"internal name {name!r}" for name in _INTERNAL_NAMES if name in lowered]
    found += [f"operator action {name!r}" for name in _OPERATOR_ACTIONS if name in lowered]
    found += [f"datastore signal {name!r}" for name in _DATASTORE_SIGNALS if name in lowered]
    return found


def assert_control_neutral(text: str, *, where: str) -> None:
    """Fail unless ``text`` is safe to hand to the model / the chat transcript."""
    violations = control_neutral_violations(text)
    assert not violations, f"{where} discloses deployment detail to the model: {violations}\n{text}"


__all__ = ["assert_control_neutral", "control_neutral_violations"]
