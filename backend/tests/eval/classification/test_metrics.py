from copy import deepcopy

import pytest

from tests.eval.classification.metrics import Sample, baseline_gate, calibration, score


def sample(
    path="financial/transactions/invoice",
    *,
    gold="financial/transactions/invoice",
    confidence=0.9,
    facets=None,
    source="synthetic",
    split="held_out",
):
    parts = path.split("/") if path else []
    return Sample(
        "case",
        gold,
        {"signed": True},
        {
            "path": path,
            "status": "classified" if path else "unclassified",
            "confidence": confidence if path else None,
            "facets": facets or {"signed": {"value": None}},
            "levels": [
                {
                    "path": "/".join(parts[:i]),
                    "confidence": confidence,
                    "probabilities": {"/".join(parts[:i]): confidence},
                }
                for i in range(1, len(parts) + 1)
            ],
            "total_cost_usd": None,
            "method": "decisions",
            "requested_model": "fixture",
        },
        30.0,
        None,
        source,
        split,
    )


def test_accuracy_denominators_keep_failures_and_wrong_parents():
    report = score([sample(), sample("legal/agreements/contract"), sample(None)], bins=10)
    assert report["per_level_accuracy"] == [1 / 3, 1 / 3, 1 / 3]
    assert report["sample_count"] == 3
    assert report["unknown_cost_documents"] == 3
    assert report["cost_usd"] is None
    assert report["facet_macro_f1"] == 0
    assert report["facet_prediction_coverage"] == 0
    assert report["path_confidence_coverage"] == 2 / 3


def test_ece_and_order_sensitivity_are_measured():
    item = sample(confidence=0.8, facets={"signed": {"value": True}})
    item.order_result = {
        "path": "legal/agreements/contract",
        "levels": [{"probabilities": {"financial": 0.2, "legal": 0.8}}],
    }
    item.result["levels"][0]["probabilities"] = {"financial": 0.8, "legal": 0.2}
    report = score([item], bins=5)
    assert report["path_ece"] == pytest.approx(0.2)
    assert report["facet_macro_f1"] == 1
    assert report["order_path_flip_rate"] == 1
    assert report["order_top_level_total_variation"] == pytest.approx(0.6)


def test_baseline_gate_fails_regression_and_mismatched_corpus():
    current = {
        "corpus_sha256": "a",
        "taxonomy_version": "1.0.0",
        "bins": 10,
        "sample_count": 4,
        "per_level_accuracy": [0.5],
    }
    previous = {**current, "per_level_accuracy": [0.9]}
    with pytest.raises(ValueError, match="regression"):
        baseline_gate(current, previous, tolerance=0.1)
    with pytest.raises(ValueError, match="corpus"):
        baseline_gate({**current, "corpus_sha256": "b"}, previous, tolerance=0.5)
    baseline_gate(current, previous, tolerance=0.4)


def test_calibration_never_approves_synthetic_or_unapproved_configuration():
    config = {
        "approved_by": "owner",
        "approved_at": "2026-10-09",
        "corpus_sha256": "a",
        "review_error_target": 0.1,
        "minimum_support": 30,
        "confidence_level": 0.95,
        "top_level_tolerance": 0.02,
        "bins": 10,
    }
    values = [sample(source="authorized_private", split="calibration") for _ in range(100)]
    values += [sample(source="authorized_private", split="held_out") for _ in range(50)]
    for i, value in enumerate(values):
        value.case_id = str(i)
    artifact = calibration(values, config=config, taxonomy_version="1.0.0", corpus_sha256="a")
    assert artifact["thresholds"][0]["threshold"] == 0.9
    assert artifact["thresholds"][0]["held_out_error"] == 0
    synthetic = deepcopy(values)
    synthetic[0].source = "synthetic"
    with pytest.raises(ValueError, match="synthetic"):
        calibration(synthetic, config=config, taxonomy_version="1.0.0", corpus_sha256="a")
    with pytest.raises(ValueError, match="approval"):
        calibration(
            values,
            config={**config, "approved_by": ""},
            taxonomy_version="1.0.0",
            corpus_sha256="a",
        )


def test_unknown_cost_is_not_zero_and_bad_measurements_rejected():
    with pytest.raises(ValueError):
        score([sample(confidence=float("nan"))], bins=10)
    with pytest.raises(ValueError):
        score([], bins=10)
    with pytest.raises(ValueError):
        score([sample()], bins=0)
