"""Run-escalation classifier — pure domain (E7-5, ADR-0015 §6, issue #239).

Pins the *routing* rule with zero I/O: which failed run terminals become
``escalated`` (a human should handle it) vs stay ``failed`` (a transient/internal
fault the retry policy re-drives), and which failures are transient (retryable
before ever reaching a terminal). The classifier is a total function of the run's
typed :class:`RunError`, so these tests are the contract for the trigger set.
"""

from __future__ import annotations

import pytest

from app.domain.entities import RunError, RunStatus
from app.domain.escalation import (
    ESCALATION_REASONS,
    classify_terminal,
    is_transient,
    normalize_reason,
    should_escalate,
)


@pytest.mark.parametrize(
    "code",
    [
        "ambiguous",
        "missing_required_input",
        "restricted_data",
        "tool_failed",
        "approval_required",
    ],
)
def test_escalation_reason_routes_failed_to_escalated(code: str) -> None:
    """E7-5: a failure a human should handle routes a ``failed`` outcome to ``escalated``."""
    error = RunError(code=code, message="needs a human")
    assert should_escalate(error) is True
    assert classify_terminal(RunStatus.FAILED, error) is RunStatus.ESCALATED


@pytest.mark.parametrize(
    "runtime_code,reason",
    [
        ("approval_denied", "approval_required"),
        ("forbidden", "restricted_data"),
        ("permission_denied", "restricted_data"),
        ("tool_error", "tool_failed"),
    ],
)
def test_runtime_error_codes_alias_to_escalation_reasons(runtime_code: str, reason: str) -> None:
    """The runtime's own terminal codes map to canonical escalation reasons."""
    assert normalize_reason(runtime_code) == reason
    assert reason in ESCALATION_REASONS
    error = RunError(code=runtime_code, message="x")
    assert classify_terminal(RunStatus.FAILED, error) is RunStatus.ESCALATED


@pytest.mark.parametrize("code", ["internal_error", "no_terminal", "run_failed"])
def test_non_escalation_failure_stays_failed(code: str) -> None:
    """Deny-by-default: an unrecognised failure stays ``failed`` (retry, not a human)."""
    error = RunError(code=code, message="boom")
    assert should_escalate(error) is False
    assert classify_terminal(RunStatus.FAILED, error) is RunStatus.FAILED


def test_success_never_escalates() -> None:
    """A clean success (no error) is never rerouted to escalated."""
    assert should_escalate(None) is False
    assert classify_terminal(RunStatus.SUCCEEDED, None) is RunStatus.SUCCEEDED


def test_escalated_input_is_unchanged() -> None:
    """classify_terminal only *promotes* a failure; an already-escalated status is kept."""
    assert (
        classify_terminal(RunStatus.ESCALATED, RunError(code="ambiguous", message="x"))
        is RunStatus.ESCALATED
    )


@pytest.mark.parametrize(
    "code", ["dependency_unavailable", "model_unavailable", "service_unavailable"]
)
def test_transient_faults_are_retryable(code: str) -> None:
    """AC-3: a transient dependency fault is worth a bounded retry before a terminal."""
    assert is_transient(RunError(code=code, message="briefly down")) is True


@pytest.mark.parametrize("code", ["ambiguous", "tool_failed", "internal_error"])
def test_escalation_and_permanent_faults_are_not_transient(code: str) -> None:
    """An escalation-worthy or permanent failure is never retried in a loop."""
    assert is_transient(RunError(code=code, message="x")) is False


def test_none_is_not_transient() -> None:
    assert is_transient(None) is False
