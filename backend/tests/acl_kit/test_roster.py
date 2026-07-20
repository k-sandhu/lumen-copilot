"""The kit's roster gates — connector-independent (F-CB-3 AC, #454).

Three things are pinned here, all of them "the kit is wired to reality":

1. **completeness** — every registered connector declaring ``map_acl`` has a kit
   subject, so landing a second managed connector without an INV-2 fixture is a
   red test rather than a silent gap;
2. **live binding** — a registered subject's proofs run through
   ``get_map_acl(get_connector(name))``, the very callable the framework invokes.
   Without this a subject could exercise a healthy helper while the connector
   *wrapper* was broken (or bypassed entirely) and every other kit test would
   still pass — the kit would be proving the wrong function;
3. **declared ≡ live** — the mapper a subject documents and the mapper production
   calls agree over every declared and generated payload, so the subject file
   stays honest documentation instead of drifting into fiction.
"""

from __future__ import annotations

import random

from app.connectors.base import get_map_acl
from app.connectors.registry import get_connector, registered_types

from .subject import REQUIRED_CASCADE_PROBE_IDS, REQUIRED_CASE_IDS, registry_mapper
from .subjects import SUBJECTS, acl_declaring_registry_types, subject_for

_EQUIVALENCE_SEED = 0x5EED11
_EQUIVALENCE_ROUNDS = 250


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


def test_every_subject_declares_the_full_cascade_probe_vocabulary() -> None:
    """Each subject can induce every §3 unprovable-set cause in its connector."""
    for subject in SUBJECTS:
        have = set(subject.cascade_probes)
        missing = set(REQUIRED_CASCADE_PROBE_IDS) - have
        assert not missing, f"{subject.name} is missing cascade probes {sorted(missing)}"
        for probe_id in REQUIRED_CASCADE_PROBE_IDS:
            assert subject.probe(probe_id).why, f"{subject.name}:{probe_id} must say what it breaks"


# --- live binding: the kit exercises the mapper production calls --------------


def test_registered_subjects_run_through_the_live_registry_mapper() -> None:
    """A registered subject's ``map_acl`` IS ``get_map_acl(get_connector(name))``.

    Identity, not merely equivalence: if the kit resolved its own import instead,
    a connector wrapper that stopped delegating — or that pre/post-processed the
    helper's result — would be invisible to every other proof in this package.
    """
    for subject in SUBJECTS:
        live = registry_mapper(subject.name)
        if not subject.is_registered:
            assert live is None, f"{subject.name} is unregistered but resolved a live mapper"
            continue
        assert live is not None, f"{subject.name} is registered but exposes no map_acl"
        assert subject.map_acl == live, (
            f"{subject.name}: the kit is not exercising the live connector mapper — "
            "AclSubject.map_acl must resolve through the registry"
        )


def test_only_the_unregistered_synthetic_subject_may_use_a_declared_mapper() -> None:
    """The fallback path exists for exactly one subject, and it is test-only."""
    fallback = [s.name for s in SUBJECTS if not s.is_registered]
    assert fallback == ["lumen-fake"], (
        f"unregistered kit subjects: {fallback} — only the synthetic generality "
        "prover may fall back to a declared mapper"
    )


def test_declared_and_live_mappers_agree_for_every_registered_subject() -> None:
    """The subject file documents the real thing, over every payload the kit uses.

    Runs both mappers across every declared fixture **and** a fresh fuzz sample:
    a wrapper that diverges from the helper on some input the hand-written cases
    happen to miss still fails here.
    """
    for subject in SUBJECTS:
        live = registry_mapper(subject.name)
        if live is None:
            continue
        rng = random.Random(_EQUIVALENCE_SEED)
        payloads = [case.raw for case in subject.cases]
        payloads += [subject.generate(rng) for _ in range(_EQUIVALENCE_ROUNDS)]
        for raw in payloads:
            declared = subject.declared_map_acl(raw, subject.context)
            actual = live(raw, subject.context)
            assert declared == actual, (
                f"{subject.name}: the declared mapper and the live connector "
                f"disagree on {raw!r} ({declared} vs {actual})"
            )
