"""The kit's roster gate — connector-independent (F-CB-3 AC, #454).

The mechanism behind "the kit accepts the next ACL-declaring connector without
modification": every registered connector that declares ``map_acl`` must have a
kit subject. Landing a second managed connector without an INV-2 fixture turns
this red, so the proof is inherited by construction rather than by remembering
to write one.
"""

from __future__ import annotations

from app.connectors.base import get_map_acl
from app.connectors.registry import get_connector, registered_types

from .subject import REQUIRED_CASE_IDS
from .subjects import SUBJECTS, acl_declaring_registry_types, subject_for


def test_every_acl_declaring_connector_has_a_kit_subject() -> None:
    missing = {name for name in acl_declaring_registry_types() if subject_for(name) is None}
    assert not missing, (
        f"connectors declare map_acl but have no tests/acl_kit subject: {sorted(missing)} — "
        "add an AclSubject (see tests/acl_kit/gdrive.py), not a new test file"
    )


def test_gdrive_is_an_acl_declaring_connector() -> None:
    """Sanity: the gate above is not vacuously true."""
    assert "gdrive" in acl_declaring_registry_types()
    assert get_map_acl(get_connector("gdrive")) is not None


def test_non_acl_connectors_are_out_of_scope() -> None:
    """``web``/upload sources keep the owner-or-grant mode — not the kit's business."""
    non_acl = registered_types() - acl_declaring_registry_types()
    assert "web" in non_acl
    assert get_map_acl(get_connector("web")) is None


def test_the_synthetic_subject_is_not_a_registered_connector() -> None:
    """The generality prover must NOT be product code (scope fence: tests only)."""
    assert "lumen-fake" not in registered_types()
    assert subject_for("lumen-fake") is not None


def test_every_subject_declares_the_full_required_vocabulary() -> None:
    """A roster-level restatement of the per-subject gate, so a half-added
    connector fails even if its parametrized cases are skipped."""
    for subject in SUBJECTS:
        have = {case.id for case in subject.cases}
        assert (
            set(REQUIRED_CASE_IDS) <= have
        ), f"{subject.name} is missing ACL cases {sorted(set(REQUIRED_CASE_IDS) - have)}"
