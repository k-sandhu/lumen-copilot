"""Admin service unit tests — static governance/risk config + cursor codec (#87).

These assert the *pure* halves of :mod:`app.services.admin_service` directly
(no DB, no app): the static risk-tier reference (spec 0004 §2.5), the
model-governance projection over the curated registry (#47, config), and the
opaque cursor codec's fail-closed behavior. The DB-backed members pagination +
the role/tenant negatives are covered end-to-end in ``test_admin_api``.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.config import get_settings
from app.core.errors import ValidationError
from app.domain.models import ModelTier
from app.services.admin_service import (
    AdminService,
    _decode_cursor,
    _encode_cursor,
)


def _service() -> AdminService:
    # The DB-free use-cases (governance, risk-tiers) never touch the session, so
    # a sentinel is sufficient here; the typed session is exercised in the API
    # tests. tenant_id is irrelevant to the tenant-agnostic reference reads.
    return AdminService(
        session=None,  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        settings=get_settings(),
    )


# --- Risk tiers (spec 0004 §2.5) -------------------------------------------


def test_risk_tiers_are_T0_through_T3_in_order() -> None:
    tiers = _service().risk_tiers()
    assert [t.tier for t in tiers] == ["T0", "T1", "T2", "T3"]
    for t in tiers:
        assert t.description
        assert t.approval


def test_risk_tier_t0_requires_no_approval() -> None:
    by_tier = {t.tier: t for t in _service().risk_tiers()}
    assert by_tier["T0"].approval == "none"
    # T2/T3 are gated on explicit human approval (out of MVP).
    assert "approval" in by_tier["T2"].approval.lower()
    assert "approval" in by_tier["T3"].approval.lower()


# --- Model governance (drawn from the curated registry, #47) ---------------


def test_model_governance_lists_registry_with_tiers() -> None:
    view = _service().model_governance()
    assert view.allowed_models, "the curated registry must not be empty"

    model_ids = {e.model_id for e in view.allowed_models}
    # The seed default surfaces (config-driven; conftest leaves the env unset).
    assert "openrouter/anthropic/claude-opus-4.8" in model_ids

    valid_tiers = {t.value for t in ModelTier}
    for entry in view.allowed_models:
        assert entry.tier in valid_tiers


def test_model_governance_describes_exactly_referenced_tiers() -> None:
    view = _service().model_governance()
    referenced = {e.tier for e in view.allowed_models}
    described = {t.id for t in view.tiers}
    # Every referenced tier is described, and no orphan tier is described.
    assert described == referenced
    for tier in view.tiers:
        assert tier.description


# --- Cursor codec (opaque; fail-closed on garbage) -------------------------


def test_cursor_roundtrips() -> None:
    member_id = uuid.uuid4()
    assert _decode_cursor(_encode_cursor(member_id)) == member_id


@pytest.mark.parametrize("bad", ["not-base64!!", "", "Zm9vYmFy", "col:" + str(uuid.uuid4())])
def test_malformed_cursor_is_rejected(bad: str) -> None:
    # Garbage, a foreign prefix, or a non-uuid payload all fail closed (→ 422).
    with pytest.raises(ValidationError):
        _decode_cursor(bad)
