"""Explicit diagnostic metrics; abstentions/unknown accounting never disappear."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from statistics import NormalDist, mean
from typing import Any


@dataclass
class Sample:
    case_id: str
    gold_path: str
    gold_facets: dict[str, bool | None]
    result: dict[str, Any]
    latency_ms: float | None
    order_result: dict[str, Any] | None = None
    source: str = "synthetic"
    split: str = "held_out"


def _probability(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("invalid probability")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("invalid probability")
    return float(value)


def _ece(values: list[tuple[float, bool]], bins: int) -> float | None:
    if not values:
        return None
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, correct in values:
        buckets[min(int(_probability(confidence) * bins), bins - 1)].append((confidence, correct))
    return sum(
        len(bucket)
        / len(values)
        * abs(mean(value[0] for value in bucket) - mean(value[1] for value in bucket))
        for bucket in buckets
        if bucket
    )


def _prefix(path: str | None, depth: int) -> str | None:
    parts = path.split("/") if path else []
    return "/".join(parts[:depth]) if len(parts) >= depth else None


def _cost(value: Any) -> Decimal | None:
    if value is None:
        return None
    cost = Decimal(str(value))
    if not cost.is_finite() or cost < 0:
        raise ValueError("invalid cost")
    return cost


def score(samples: list[Sample], *, bins: int) -> dict[str, Any]:
    if not samples or isinstance(bins, bool) or not 1 <= bins <= 100:
        raise ValueError("empty corpus or invalid calibration bins")
    depth = max(len(s.gold_path.split("/")) for s in samples)
    accuracy, denominators, level_ece, level_coverage = [], [], [], []
    for level in range(1, depth + 1):
        relevant = [s for s in samples if _prefix(s.gold_path, level) is not None]
        correct = [
            _prefix(s.result.get("path"), level) == _prefix(s.gold_path, level) for s in relevant
        ]
        accuracy.append(mean(correct))
        denominators.append(len(relevant))
        confidence_values = []
        for s, ok in zip(relevant, correct, strict=True):
            levels = s.result.get("levels", [])
            if len(levels) >= level and levels[level - 1].get("confidence") is not None:
                confidence_values.append((_probability(levels[level - 1]["confidence"]), ok))
        level_ece.append(_ece(confidence_values, bins))
        level_coverage.append(len(confidence_values) / len(relevant))
    facets: dict[str, float | None] = {}
    known, predicted = 0, 0
    for facet in sorted({key for s in samples for key in s.gold_facets}):
        tp = fp = fn = 0
        for sample in samples:
            truth = sample.gold_facets.get(facet)
            if truth is None:
                continue
            known += 1
            value = sample.result.get("facets", {}).get(facet, {}).get("value")
            if value is not None and type(value) is not bool:
                raise ValueError("invalid facet value")
            predicted += value is not None
            tp += truth is True and value is True
            fp += truth is False and value is True
            fn += truth is True and value is not True
        denominator = 2 * tp + fp + fn
        facets[facet] = 2 * tp / denominator if denominator else None
    confidence_values = [
        (
            _probability(s.result["confidence"]),
            s.result.get("path") == s.gold_path and s.result.get("status") == "classified",
        )
        for s in samples
        if s.result.get("confidence") is not None
    ]
    paired = [s for s in samples if s.order_result is not None]
    variations = []
    for sample in paired:
        assert sample.order_result is not None
        left, right = sample.result.get("levels", []), sample.order_result.get("levels", [])
        if left and right:
            p, q = left[0]["probabilities"], right[0]["probabilities"]
            variations.append(
                0.5
                * sum(
                    abs(_probability(p.get(k, 0)) - _probability(q.get(k, 0)))
                    for k in set(p) | set(q)
                )
            )
    latencies = sorted(s.latency_ms for s in samples if s.latency_ms is not None)
    if any(not math.isfinite(value) or value < 0 for value in latencies):
        raise ValueError("invalid latency")
    costs = [_cost(s.result.get("total_cost_usd")) for s in samples]
    known_cost = sum((c for c in costs if c is not None), Decimal(0))
    f1_values = [value for value in facets.values() if value is not None]
    return {
        "metric_definition": "classification-eval-1",
        "bins": bins,
        "sample_count": len(samples),
        "per_level_accuracy": accuracy,
        "per_level_denominators": denominators,
        "per_level_ece": level_ece,
        "per_level_confidence_coverage": level_coverage,
        "facet_f1": facets,
        "facet_macro_f1": mean(f1_values) if f1_values else None,
        "facet_prediction_coverage": predicted / known if known else None,
        "path_ece": _ece(confidence_values, bins),
        "path_confidence_coverage": len(confidence_values) / len(samples),
        "order_pairs": len(paired),
        "order_path_flip_rate": mean(
            s.result.get("path") != s.order_result.get("path") for s in paired
        )
        if paired
        else None,
        "order_top_level_total_variation": mean(variations) if variations else None,
        "latency_mean_ms": mean(latencies) if latencies else None,
        "latency_p95_ms": latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else None,
        "known_cost_usd": str(known_cost),
        "unknown_cost_documents": costs.count(None),
        "cost_usd": str(known_cost) if None not in costs else None,
    }


def baseline_gate(current: dict[str, Any], previous: dict[str, Any], *, tolerance: float) -> None:
    _probability(tolerance)
    for key in ("corpus_sha256", "taxonomy_version", "bins", "sample_count", "metric_definition"):
        if current.get(key) != previous.get(key):
            raise ValueError(f"baseline {key} mismatch")
    candidate = _probability(current["per_level_accuracy"][0])
    baseline = _probability(previous["per_level_accuracy"][0])
    if baseline - candidate > tolerance + 1e-12:
        raise ValueError("top-level accuracy regression exceeds approved tolerance")


def _wilson_upper(errors: int, count: int, confidence_level: float) -> float:
    z = NormalDist().inv_cdf((1 + confidence_level) / 2)
    p = errors / count
    return (
        p + z * z / (2 * count) + z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count))
    ) / (1 + z * z / count)


def calibration(
    samples: list[Sample], *, config: dict[str, Any], taxonomy_version: str, corpus_sha256: str
) -> dict[str, Any]:
    from datetime import date

    if not config.get("approved_by") or not config.get("approved_at"):
        raise ValueError("owner approval required")
    date.fromisoformat(config["approved_at"])
    if config.get("corpus_sha256") != corpus_sha256:
        raise ValueError("approval corpus mismatch")
    target = _probability(config["review_error_target"])
    _probability(config["top_level_tolerance"])
    confidence = _probability(config["confidence_level"])
    support = config["minimum_support"]
    if (
        confidence in (0, 1)
        or isinstance(support, bool)
        or not isinstance(support, int)
        or support < 1
    ):
        raise ValueError("invalid calibration configuration")
    if any(s.source != "authorized_private" for s in samples):
        raise ValueError("synthetic diagnostics cannot approve release calibration")
    if len({s.case_id for s in samples}) != len(samples):
        raise ValueError("calibration and held-out examples must be distinct")
    score(samples, bins=config["bins"])
    groups = {(s.result.get("method"), s.result.get("requested_model")) for s in samples}
    thresholds = []
    for method, model in sorted(groups, key=str):
        group = [
            s
            for s in samples
            if (s.result.get("method"), s.result.get("requested_model")) == (method, model)
        ]
        train = [
            s for s in group if s.split == "calibration" and s.result.get("confidence") is not None
        ]
        threshold = None
        bound = None
        count = 0
        for candidate in sorted({_probability(s.result["confidence"]) for s in train}):
            accepted = [s for s in train if s.result["confidence"] >= candidate]
            errors = sum(
                s.result.get("path") != s.gold_path or s.result.get("status") != "classified"
                for s in accepted
            )
            upper = _wilson_upper(errors, len(accepted), confidence)
            if len(accepted) >= support and upper <= target:
                threshold, bound, count = candidate, upper, len(accepted)
                break
        held = [
            s
            for s in group
            if s.split == "held_out"
            and threshold is not None
            and s.result.get("confidence") is not None
            and s.result["confidence"] >= threshold
        ]
        held_errors = sum(
            s.result.get("path") != s.gold_path or s.result.get("status") != "classified"
            for s in held
        )
        # No supported held-out verification means no approved threshold.
        held_bound = _wilson_upper(held_errors, len(held), confidence) if held else None
        validated = len(held) >= support and held_bound is not None and held_bound <= target
        thresholds.append(
            {
                "method": method,
                "model": model,
                "threshold": threshold if validated else None,
                "calibration_count": count,
                "calibration_upper_error": bound,
                "held_out_count": len(held),
                "held_out_error": held_errors / len(held) if held else None,
                "held_out_upper_error": held_bound,
                "validated": validated,
            }
        )
    return {
        "taxonomy_version": taxonomy_version,
        "corpus_sha256": corpus_sha256,
        "metric_definition": "classification-eval-1",
        "approval": config,
        "thresholds": thresholds,
    }
