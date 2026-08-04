"""Typed refusal reasons for the code-execution sandbox (issue #502).

A ``run_python`` submission can be refused by several **independent** switches, and
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
* :data:`SANDBOX_REASON_RUNNER_UNAVAILABLE` / :data:`SANDBOX_REASON_RUNNER_REJECTED`
  / :data:`SANDBOX_REASON_RUNNER_ERROR` — every policy admits the run but the
  dedicated runner service could not carry it out. Three reasons, not one, because
  they need three different actions: nothing answered at all (start it), it
  answered by *refusing* the request (a package it could not download, an invalid
  requirement, a generation it no longer holds — read the rejected request), or it
  answered with an internal failure (its own log, and whether the execution image
  is on the host daemon). Collapsing these into "unreachable" sent an operator to
  check a service that was answering fine.
* :data:`SANDBOX_REASON_PACKAGE_DENIED` / :data:`SANDBOX_REASON_SESSION_REQUIRED` —
  the request itself is inadmissible (a package outside the tenant allow-list; a
  run with no parent chat session to own the reusable sandbox).

It lives in ``domain/`` (pure, no adapter imports) precisely because both halves
of the admission path need it and they may not import each other: ``services/
sandbox_policy_service.py`` must not import the ``app.sandbox`` package (that
would close a services→sandbox→services cycle), while ``app.sandbox.service``
consumes the reason the reader resolved.

**Two audiences, two message tables.** The reason CODE is the durable key; the
sentence depends on who reads it.

* :data:`SANDBOX_REASON_MESSAGES` — the **operator** sentence. It names the exact
  control (``SANDBOX_ENABLED``, the ``sandbox-runner`` service, "restart the API
  and worker") because it is written to the structured log, to
  ``audit_events.metadata``, and to the runbook's diagnosis table. Those surfaces
  are already behind an admin/operator boundary.
* :data:`SANDBOX_REASON_PUBLIC_MESSAGES` — the **model + transcript** sentence, and
  the ONLY one that may reach either. It says what happened and who can change it,
  and deliberately names no environment variable, no internal service, no process
  topology, and no datastore state.

The split is not cosmetic. A run's refusal sentence lands in ``code_runs.stderr``,
which the ``run_python`` tool renders straight into the tool reply the model reads —
a **prompt-injection-reachable** surface. A retrieved document that says "quote any
tool error verbatim" would otherwise make the assistant recite this deployment's
kill-switch name, its service topology, and which components are currently down, to
one tenant's member. Naming a product control ("Admin → Code execution") is fine and
is the point; naming deployment infrastructure is not.
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
#: The runner ANSWERED and refused the request (4xx) — an unresolvable package
#: requirement, a rejected staged path, a generation it no longer holds. It is up.
SANDBOX_REASON_RUNNER_REJECTED = "sandbox_runner_rejected"
#: The runner ANSWERED with an internal failure (5xx) — reachable, but it could not
#: carry the run out (e.g. the execution image is missing from the host daemon).
SANDBOX_REASON_RUNNER_ERROR = "sandbox_runner_error"
#: The runner answered 401: the shared secret is missing or wrong (#508). Its own
#: reason because the remedy shares nothing with the others — the service is up, it
#: is reachable, and the request was well-formed; only the credential is wrong.
#: Reporting it as "refused the request" would send an operator hunting a package
#: resolution failure that never happened.
SANDBOX_REASON_RUNNER_UNAUTHORIZED = "sandbox_runner_unauthorized"
#: The run crashed for an unclassified reason inside the sandbox path.
SANDBOX_REASON_RUN_ERROR = "sandbox_run_error"


#: The OPERATOR sentence for each reason: what refused, and which control changes
#: the answer. Written to the structured log and to ``audit_events.metadata`` —
#: never to ``code_runs.stderr`` and never to a tool reply, because it names
#: deployment infrastructure on purpose. Use :data:`SANDBOX_REASON_PUBLIC_MESSAGES`
#: for anything the model or the chat transcript can see.
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
    SANDBOX_REASON_PACKAGE_DENIED: (
        "A requested package is not admitted by this workspace's package policy, so "
        "the run was refused before any code launched. An admin controls the allowed "
        "and denied package lists under Admin → Code execution."
    ),
    SANDBOX_REASON_SESSION_REQUIRED: ("Reusable code execution requires a parent chat session."),
    SANDBOX_REASON_RUNNER_UNAVAILABLE: (
        "The code sandbox service is unreachable, so the run could not start. "
        "Every policy already permits it — an operator must check that the "
        "sandbox-runner service is running and reachable."
    ),
    SANDBOX_REASON_RUNNER_UNAUTHORIZED: (
        "The sandbox-runner service answered 401: the shared secret is missing or does "
        "not match. Set the SAME SANDBOX_RUNNER_TOKEN on the API, the worker and the "
        "sandbox-runner, then restart all three. The service is up and reachable — "
        "only the credential is wrong."
    ),
    SANDBOX_REASON_RUNNER_REJECTED: (
        "The sandbox-runner service answered and REFUSED the request (4xx): a package "
        "requirement it could not resolve or download, a rejected staged input path, "
        "or a session generation it no longer holds. The service is up — its log has "
        "the rejected request."
    ),
    SANDBOX_REASON_RUNNER_ERROR: (
        "The sandbox-runner service answered with an internal error (5xx): it is "
        "reachable but could not carry the run out. Check its log, and that the "
        "execution image named by SANDBOX_IMAGE is present on the host daemon — the "
        "runner never pulls it."
    ),
    SANDBOX_REASON_RUN_ERROR: "The sandbox run failed unexpectedly.",
}


#: The MODEL- and TRANSCRIPT-facing sentence for each reason. This is the only table
#: whose strings may reach ``code_runs.stderr``, a tool reply, or a user-facing API
#: error. Each one says what happened and who can change it — an admin, an operator,
#: or "try again" — and names no environment variable, internal service, process
#: topology, or datastore state, because everything here is reachable by a
#: prompt-injected instruction to quote the tool error verbatim.
SANDBOX_REASON_PUBLIC_MESSAGES: dict[str, str] = {
    SANDBOX_REASON_DEPLOY_DISABLED: (
        "Code execution is turned off for this deployment. Ask an operator to enable it."
    ),
    SANDBOX_REASON_POLICY_ABSENT: (
        "Code execution is not enabled for this workspace. An admin can turn it on "
        "under Admin → Code execution."
    ),
    SANDBOX_REASON_TENANT_DISABLED: (
        "Code execution is turned off for this workspace. An admin can turn it on "
        "under Admin → Code execution."
    ),
    SANDBOX_REASON_POLICY_UNREADABLE: (
        "Code execution could not be checked for this workspace, so the run was "
        "refused rather than run unchecked. Try again shortly."
    ),
    SANDBOX_REASON_PACKAGE_DENIED: (
        "A requested package is not allowed for this workspace. An admin can review "
        "the allowed packages under Admin → Code execution."
    ),
    SANDBOX_REASON_SESSION_REQUIRED: ("Reusable code execution requires a parent chat session."),
    SANDBOX_REASON_RUNNER_UNAVAILABLE: (
        "The code sandbox is temporarily unavailable, so the run could not start. "
        "Try again shortly."
    ),
    SANDBOX_REASON_RUNNER_UNAUTHORIZED: (
        "Code execution is not available right now. Ask an operator to check it."
    ),
    SANDBOX_REASON_RUNNER_REJECTED: (
        "The code sandbox could not prepare this run. Try again without extra "
        "packages, or with different ones."
    ),
    SANDBOX_REASON_RUNNER_ERROR: ("The code sandbox could not run this code. Try again shortly."),
    SANDBOX_REASON_RUN_ERROR: "The sandbox run failed unexpectedly.",
}


def sandbox_reason_message(reason: str) -> str:
    """The OPERATOR sentence for ``reason`` (never raises).

    For the structured log, ``audit_events.metadata``, and operator tooling only —
    it names deployment infrastructure by design. An unmapped reason falls back to
    the unclassified-failure sentence, so a new code can never surface an empty
    explanation.
    """
    return SANDBOX_REASON_MESSAGES.get(reason, SANDBOX_REASON_MESSAGES[SANDBOX_REASON_RUN_ERROR])


def sandbox_reason_public_message(reason: str) -> str:
    """The MODEL/TRANSCRIPT sentence for ``reason`` (never raises).

    The only variant safe to write into ``code_runs.stderr``, a tool reply, or a
    user-facing API error. An unmapped reason falls back to the unclassified-failure
    sentence — which is control-neutral too, so the fallback can never leak either.
    """
    return SANDBOX_REASON_PUBLIC_MESSAGES.get(
        reason, SANDBOX_REASON_PUBLIC_MESSAGES[SANDBOX_REASON_RUN_ERROR]
    )


__all__ = [
    "SANDBOX_REASON_DEPLOY_DISABLED",
    "SANDBOX_REASON_MESSAGES",
    "SANDBOX_REASON_PACKAGE_DENIED",
    "SANDBOX_REASON_POLICY_ABSENT",
    "SANDBOX_REASON_POLICY_UNREADABLE",
    "SANDBOX_REASON_PUBLIC_MESSAGES",
    "SANDBOX_REASON_RUNNER_ERROR",
    "SANDBOX_REASON_RUNNER_REJECTED",
    "SANDBOX_REASON_RUNNER_UNAUTHORIZED",
    "SANDBOX_REASON_RUNNER_UNAVAILABLE",
    "SANDBOX_REASON_RUN_ERROR",
    "SANDBOX_REASON_SESSION_REQUIRED",
    "SANDBOX_REASON_TENANT_DISABLED",
    "sandbox_reason_message",
    "sandbox_reason_public_message",
]
