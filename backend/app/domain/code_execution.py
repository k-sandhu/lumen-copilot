"""Typed refusal reasons for the code-execution sandbox (issue #502).

A ``run_python`` submission can be refused by four **independent** switches, and
before #502 all of them collapsed into one opaque "code execution is disabled"
line — so an operator staring at a blocked run had no way to tell which control
to flip. This module is the pure vocabulary that makes each refusal say *itself*:

* :data:`SANDBOX_REASON_DEPLOY_DISABLED` — the deploy-wide kill-switch
  (``SANDBOX_ENABLED``, ``core/config.py``). Dominant: no tenant can run code
  while it is off, however the per-tenant policy is set.
* :data:`SANDBOX_REASON_POLICY_ABSENT` / :data:`SANDBOX_REASON_TENANT_DISABLED` —
  the per-tenant sandbox policy row (``services/sandbox_policy_service.py``):
  never configured (deny-by-default) versus explicitly turned off. Different
  admin actions fix them, so they are different reasons.
* :data:`SANDBOX_REASON_POLICY_UNREADABLE` — the policy could not be read, so the
  run was refused fail-closed (INV-7). An infrastructure fault, NOT a setting.
* :data:`SANDBOX_REASON_RUNNER_UNAVAILABLE` — every policy admits the run but the
  dedicated ``sandbox-runner`` service is unreachable. Not a policy at all.
* :data:`SANDBOX_REASON_PACKAGE_DENIED` / :data:`SANDBOX_REASON_SESSION_REQUIRED` —
  the request itself is inadmissible (a package outside the tenant allow-list; a
  run with no parent chat session to own the reusable sandbox).

It lives in ``domain/`` (pure, no adapter imports) precisely because both halves
of the admission path need it and they may not import each other: ``services/
sandbox_policy_service.py`` must not import the ``app.sandbox`` package (that
would close a services→sandbox→services cycle), while ``app.sandbox.service``
consumes the reason the reader resolved.

**Model-safe by construction.** Every message names a *control* ("the deployment's
SANDBOX_ENABLED switch", "Admin → Code execution") and never tenant-internal or
security-sensitive detail — these strings are surfaced verbatim to the model in
the ``run_python`` tool reply, and to the user in the chat transcript.
"""

from __future__ import annotations

#: The deploy-wide kill-switch (``SANDBOX_ENABLED``) is off — nothing runs.
SANDBOX_REASON_DEPLOY_DISABLED = "sandbox_disabled_deploy"
#: This tenant has no sandbox policy row at all (deny-by-default, issue #233).
SANDBOX_REASON_POLICY_ABSENT = "sandbox_policy_absent"
#: A stored per-tenant policy exists and says ``enabled=false``.
SANDBOX_REASON_TENANT_DISABLED = "sandbox_disabled_tenant"
#: The per-tenant policy could not be read; the run was refused (fail-closed).
SANDBOX_REASON_POLICY_UNREADABLE = "sandbox_policy_unreadable"
#: A requested package is outside the tenant's package policy.
SANDBOX_REASON_PACKAGE_DENIED = "sandbox_package_denied"
#: The run has no parent chat session to own the reusable sandbox.
SANDBOX_REASON_SESSION_REQUIRED = "sandbox_session_required"
#: The dedicated runner service is unreachable (a dependency fault, not a policy).
SANDBOX_REASON_RUNNER_UNAVAILABLE = "sandbox_runner_unavailable"
#: The run crashed for an unclassified reason inside the sandbox path.
SANDBOX_REASON_RUN_ERROR = "sandbox_run_error"


#: The operator-actionable sentence for each reason: what refused, and which
#: control changes the answer. Rendered into the ``code_runs.stderr`` of the
#: terminal row, which is what the tool replies to the model with — so it must
#: stay free of tenant-internal detail.
SANDBOX_REASON_MESSAGES: dict[str, str] = {
    SANDBOX_REASON_DEPLOY_DISABLED: (
        "Code execution is turned off for this deployment: the SANDBOX_ENABLED "
        "switch is off, so no workspace can run Python. An operator must enable it "
        "and restart the API and worker."
    ),
    SANDBOX_REASON_POLICY_ABSENT: (
        "This workspace has no sandbox policy, so code execution is off by default. "
        "An admin must turn it on under Admin → Code execution."
    ),
    SANDBOX_REASON_TENANT_DISABLED: (
        "Code execution is switched off in this workspace's sandbox policy. "
        "An admin can turn it on under Admin → Code execution."
    ),
    SANDBOX_REASON_POLICY_UNREADABLE: (
        "The sandbox policy could not be read, so the run was refused rather than "
        "run unchecked. Retry once the policy store is reachable."
    ),
    SANDBOX_REASON_SESSION_REQUIRED: ("Reusable code execution requires a parent chat session."),
    SANDBOX_REASON_RUNNER_UNAVAILABLE: (
        "The code sandbox service is unreachable, so the run could not start. "
        "Every policy already permits it — an operator must check that the "
        "sandbox-runner service is running and reachable."
    ),
    SANDBOX_REASON_RUN_ERROR: "The sandbox run failed unexpectedly.",
}


def sandbox_reason_message(reason: str) -> str:
    """The operator-actionable sentence for ``reason`` (never raises).

    An unmapped reason falls back to the unclassified-failure sentence, so a new
    code can never surface an empty explanation to the model.
    """
    return SANDBOX_REASON_MESSAGES.get(reason, SANDBOX_REASON_MESSAGES[SANDBOX_REASON_RUN_ERROR])


__all__ = [
    "SANDBOX_REASON_DEPLOY_DISABLED",
    "SANDBOX_REASON_MESSAGES",
    "SANDBOX_REASON_PACKAGE_DENIED",
    "SANDBOX_REASON_POLICY_ABSENT",
    "SANDBOX_REASON_POLICY_UNREADABLE",
    "SANDBOX_REASON_RUNNER_UNAVAILABLE",
    "SANDBOX_REASON_RUN_ERROR",
    "SANDBOX_REASON_SESSION_REQUIRED",
    "SANDBOX_REASON_TENANT_DISABLED",
    "sandbox_reason_message",
]
