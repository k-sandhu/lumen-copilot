"""The two #502 message tables: complete, and split by audience.

``domain/code_execution`` keys every sandbox refusal off a typed ``reason_code`` and
renders it twice — once for the operator (log + audit), once for the model and the
chat transcript. Two things have to hold, and neither is visible from a call site:

1. **Completeness.** Every declared ``SANDBOX_REASON_*`` code is a key of BOTH
   tables. ``SANDBOX_REASON_PACKAGE_DENIED`` shipped declared, exported and
   documented but *absent* from the map, so ``sandbox_reason_message()`` answered it
   with "The sandbox run failed unexpectedly." — contradicting its own docstring. It
   was latent only because the package path happens to pass ``exc.detail`` instead.
2. **The split is real.** The operator table still names the exact control; the
   public table names none of it. A "fix" that simply deleted the specifics from
   both would pass a one-sided disclosure test, so both directions are asserted.
"""

from __future__ import annotations

import app.domain.code_execution as code_execution
from app.domain.code_execution import (
    SANDBOX_REASON_DEPLOY_DISABLED,
    SANDBOX_REASON_MESSAGES,
    SANDBOX_REASON_PACKAGE_DENIED,
    SANDBOX_REASON_PUBLIC_MESSAGES,
    SANDBOX_REASON_RUN_ERROR,
    SANDBOX_REASON_RUNNER_UNAVAILABLE,
    sandbox_reason_message,
    sandbox_reason_public_message,
)
from tests._disclosure import assert_control_neutral, control_neutral_violations


def _declared_reason_codes() -> dict[str, str]:
    """Every ``SANDBOX_REASON_*`` constant that is a reason CODE (not a table)."""
    return {
        name: value
        for name, value in vars(code_execution).items()
        if name.startswith("SANDBOX_REASON_") and isinstance(value, str)
    }


def test_every_declared_reason_code_has_an_operator_and_a_public_message() -> None:
    """No declared reason may fall through to the unclassified-failure sentence."""
    declared = _declared_reason_codes()
    # Sanity: the enumeration actually found the codes (a rename must not silently
    # empty this test out).
    assert len(declared) >= 8
    missing_operator = {
        name: value for name, value in declared.items() if value not in SANDBOX_REASON_MESSAGES
    }
    missing_public = {
        name: value
        for name, value in declared.items()
        if value not in SANDBOX_REASON_PUBLIC_MESSAGES
    }
    assert missing_operator == {}
    assert missing_public == {}


def test_package_denied_is_not_answered_with_the_unclassified_fallback() -> None:
    """The specific regression: a declared code that resolved to "failed unexpectedly"."""
    fallback = SANDBOX_REASON_MESSAGES[SANDBOX_REASON_RUN_ERROR]
    assert sandbox_reason_message(SANDBOX_REASON_PACKAGE_DENIED) != fallback
    assert "package" in sandbox_reason_message(SANDBOX_REASON_PACKAGE_DENIED).lower()
    public_fallback = SANDBOX_REASON_PUBLIC_MESSAGES[SANDBOX_REASON_RUN_ERROR]
    assert sandbox_reason_public_message(SANDBOX_REASON_PACKAGE_DENIED) != public_fallback


def test_no_public_message_discloses_deployment_infrastructure() -> None:
    """The model-visible table names no env var, internal host, or restart step."""
    for reason, message in SANDBOX_REASON_PUBLIC_MESSAGES.items():
        assert_control_neutral(message, where=f"SANDBOX_REASON_PUBLIC_MESSAGES[{reason!r}]")


def test_an_unknown_reason_falls_back_to_a_control_neutral_sentence() -> None:
    """A code added without a public entry must degrade safely, not leak."""
    assert_control_neutral(
        sandbox_reason_public_message("some_code_nobody_mapped_yet"),
        where="sandbox_reason_public_message(unknown)",
    )


def test_the_operator_table_still_names_the_exact_control() -> None:
    """The split must be a split, not a deletion — the operator sentence is specific."""
    deploy = sandbox_reason_message(SANDBOX_REASON_DEPLOY_DISABLED)
    assert "SANDBOX_ENABLED" in deploy
    assert "restart" in deploy.lower()
    assert "sandbox-runner" in sandbox_reason_message(SANDBOX_REASON_RUNNER_UNAVAILABLE)
    # …and precisely those specifics are what the public sentence must NOT carry.
    assert "SANDBOX_ENABLED" not in sandbox_reason_public_message(SANDBOX_REASON_DEPLOY_DISABLED)
    assert "sandbox-runner" not in sandbox_reason_public_message(SANDBOX_REASON_RUNNER_UNAVAILABLE)


def test_the_two_tables_disagree_wherever_the_operator_sentence_is_specific() -> None:
    """A reason whose operator sentence leaks must have a DIFFERENT public sentence.

    Guards the failure mode where a new reason is added to both tables by copying
    one string into the other — completeness would pass while the leak returns.
    """
    for reason, operator in SANDBOX_REASON_MESSAGES.items():
        if control_neutral_violations(operator):
            assert SANDBOX_REASON_PUBLIC_MESSAGES[reason] != operator
