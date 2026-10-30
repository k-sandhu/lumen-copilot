"""Run escalation classification — pure domain (E7-5, ADR-0015 §6, issue #239).

A headless run reaches a terminal :class:`~app.domain.entities.RunStatus`. This
module decides *which* terminal: a run that failed for a reason **a human should
handle** (ambiguity, missing required input, restricted data, an unrecoverable
tool failure, or an approval that has no approver) ends ``escalated`` — a
first-class terminal that keeps a human in the loop — rather than ``failed``. A
run that failed for a **transient / infrastructural** reason (model briefly
unavailable, an internal defect) stays ``failed`` so the retry policy (bounded
backoff, one layer up) can re-drive it before a human is ever paged.

Pure, framework-free (backend/AGENTS.md: ``domain/`` is pure): the classifier is a
total function of the run's terminal :class:`~app.domain.entities.RunError` code, so
the same input always yields the same routing and it is trivially unit-testable.
The escalation *shape* (``escalated`` is a status, ``error`` carries the reason) is
fixed by ADR-0015 §6; this module owns the *triggers* the ADR handed to #239.

**Read-before-write (mission filter #3, INV-7):** escalation is the deny-by-default
path — when the agent cannot decide *safely* (an ambiguous request, data it may not
read, a write it cannot get approved), it stops and hands off to a human rather than
guessing or acting. A run is **never silently dropped**: every terminal is queryable,
and an escalation additionally notifies the owner (the service layer).
"""

from __future__ import annotations

import re
from typing import Final

from app.domain.entities import RunError, RunStatus

# --- Escalation reason codes ------------------------------------------------
# The stable ``error.code`` values that route a failed run to ``escalated`` rather
# than ``failed``. Each names a condition E7-5 enumerates as "a human should decide":
# the agent stops instead of guessing or acting beyond its authority.

#: The request/grounding was ambiguous or conflicting — the agent will not guess.
ESCALATION_AMBIGUOUS: Final = "ambiguous"
#: A required input was missing — the run cannot proceed without a human filling it.
ESCALATION_MISSING_INPUT: Final = "missing_required_input"
#: The run needed data the owner may not read (INV-2) — a human must decide.
ESCALATION_RESTRICTED_DATA: Final = "restricted_data"
#: An unrecoverable tool failure — the agent cannot complete the task via tools.
ESCALATION_TOOL_FAILED: Final = "tool_failed"
#: A consequential (T2+) action needs approval and none is available (INV-7).
ESCALATION_APPROVAL_REQUIRED: Final = "approval_required"

#: The full set of reason codes that escalate (deny-by-default: anything not here
#: that still failed stays ``failed`` for the retry policy to re-drive).
ESCALATION_REASONS: Final[frozenset[str]] = frozenset(
    {
        ESCALATION_AMBIGUOUS,
        ESCALATION_MISSING_INPUT,
        ESCALATION_RESTRICTED_DATA,
        ESCALATION_TOOL_FAILED,
        ESCALATION_APPROVAL_REQUIRED,
    }
)

# The runtime maps its own terminal ``error`` codes to these reasons. A run whose
# tool call was denied by the approval gate (INV-7 — no approver on the headless
# path) surfaces as ``approval_denied``; a restricted-data / permission stop as
# ``forbidden`` / ``permission.denied``. This alias table lets the classifier route
# those without the runtime having to know about escalation.
_RUNTIME_CODE_ALIASES: Final[dict[str, str]] = {
    "approval_denied": ESCALATION_APPROVAL_REQUIRED,
    "forbidden": ESCALATION_RESTRICTED_DATA,
    "permission_denied": ESCALATION_RESTRICTED_DATA,
    "tool_error": ESCALATION_TOOL_FAILED,
}


# The terminal ``error.code`` values that name a **transient** fault (a briefly
# unavailable dependency) — the run should be re-driven with backoff before it ever
# reaches a terminal (ADR-0015 §5, AC-3). Deny-by-default: anything not here is
# permanent, so a genuine defect / escalation-worthy stop is never retried in a loop.
_TRANSIENT_REASONS: Final[frozenset[str]] = frozenset(
    {
        "dependency_unavailable",
        "model_unavailable",
        "service_unavailable",
    }
)


def normalize_reason(code: str) -> str:
    """Map a runtime terminal ``error.code`` to a canonical escalation reason (or itself)."""
    return _RUNTIME_CODE_ALIASES.get(code, code)


def is_transient(error: RunError | None) -> bool:
    """True when a failure is a transient dependency fault worth a bounded retry (AC-3).

    Deny-by-default: only a recognised transient reason retries; every other failure
    (a defect, or an escalation-worthy stop) is permanent so it reaches a terminal on
    the first attempt rather than looping. ``None`` (a success) is never transient.
    """
    if error is None:
        return False
    return error.code in _TRANSIENT_REASONS


def should_escalate(error: RunError | None) -> bool:
    """True when a failed run should route to ``escalated`` (a human handles it).

    Deny-by-default: only a recognised escalation reason (or a runtime alias of one)
    escalates; every other failure stays ``failed`` so the retry policy can re-drive
    a transient fault before a human is paged. ``None`` (a clean success) never
    escalates.
    """
    if error is None:
        return False
    return normalize_reason(error.code) in ESCALATION_REASONS


# --- Tool-failure classification (#578) --------------------------------------
# A headless run whose stream ended ``done`` can still have failed at its job:
# a tool call that was **denied or unavailable** (code execution refused, a
# policy/approval denial, a tool with no registered adapter) means the
# assistant could not do what it was configured to do, so the run is Failed
# with this reason rather than a false Succeeded. Other tool errors — bad
# arguments, a timeout, a transient handler error the model recovered from —
# do not hard-fail the run; the failed calls stay visible in the transcript.
# This is the single classification the success rule hangs on: reversing the
# decision means changing :func:`is_denied_or_unavailable_tool_error` only.

#: The ``error.code`` a run carries when a denied/unavailable tool fails it.
TOOL_FAILURE_REASON: Final = "tool_failure"

# The denied/unavailable codes the tool runner emits today (``domain.tools``
# ``ERROR_*`` + the code-execution tool). Matched by meaning below as well, so
# a synonym the runner grows later (``sandbox_unavailable``, ``*_disabled``)
# fails closed instead of slipping through as a recoverable error.
_DENIED_TOOL_ERRORS: Final[frozenset[str]] = frozenset(
    {
        "approval_denied",
        "autonomy_denied",
        "code_execution_denied",
        "tool_not_found",
        "tool_not_permitted",
    }
)
_DENIED_TOOL_TOKENS: Final[frozenset[str]] = frozenset(
    {"denied", "disabled", "forbidden", "unavailable"}
)


def is_denied_or_unavailable_tool_error(code: str | None) -> bool:
    """True when a failed tool result's ``error`` code hard-fails the run (#578).

    Denied/unavailable by meaning, not only by enumeration: an exact known
    denial code, a ``*_not_permitted`` code, a missing-adapter code, or any
    code carrying a denied/disabled/forbidden/unavailable token. A failed
    tool result with **no** error code is unclassifiable, so it fails closed
    (True) — the sink only calls this for ``ok=False`` results, where the
    runner guarantees a code. Everything else (bad args, timeout, a generic
    ``tool_error``) is recoverable and returns False.
    """
    if code is None or not code.strip():
        return True
    normalized = code.strip().lower()
    # A transient provider outage is recoverable if the model completed via
    # another route/tool. "Unavailable" here describes the dependency's health,
    # rather than an absent adapter or a denied capability.
    if normalized in _TRANSIENT_REASONS:
        return False
    if normalized in _DENIED_TOOL_ERRORS:
        return True
    if normalized == "not_permitted" or normalized.endswith("_not_permitted"):
        return True
    tokens = frozenset(t for t in re.split(r"[^a-z0-9]+", normalized) if t)
    if tokens & _DENIED_TOOL_TOKENS:
        return True
    return "missing" in tokens and bool(tokens & {"adapter", "tool"})


def classify_terminal(status: RunStatus, error: RunError | None) -> RunStatus:
    """Route a run's *provisional* terminal to its final status (E7-5).

    A ``FAILED`` outcome whose reason a human should handle becomes ``ESCALATED``
    (with the same typed ``error`` carried forward as the escalation reason);
    every other outcome (a success, or a transient/internal failure) is unchanged.
    A total function — the same input always yields the same terminal.
    """
    if status is RunStatus.FAILED and should_escalate(error):
        return RunStatus.ESCALATED
    return status


__all__ = [
    "ESCALATION_AMBIGUOUS",
    "ESCALATION_APPROVAL_REQUIRED",
    "ESCALATION_MISSING_INPUT",
    "ESCALATION_REASONS",
    "ESCALATION_RESTRICTED_DATA",
    "ESCALATION_TOOL_FAILED",
    "TOOL_FAILURE_REASON",
    "classify_terminal",
    "is_denied_or_unavailable_tool_error",
    "is_transient",
    "normalize_reason",
    "should_escalate",
]
