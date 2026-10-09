import json
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from app.domain.decisions import DecisionAttempt, DecisionPolicy
from app.llm.decisions import DecisionError
from tests.eval.classification import harness
from tests.eval.classification.harness import DATA, TENANT, EvaluationLedger, offline


async def test_recorded_replay_exercises_cascade_fallback_and_order_without_network():
    report, samples = await offline(bins=10)
    assert report["release_calibration"] is None
    assert report["reports"]["rules"]["per_level_accuracy"] == [0.25] * 3
    assert report["reports"]["decisions"]["per_level_accuracy"] == [1.0] * 3
    assert report["reports"]["decisions"]["order_path_flip_rate"] == 0.25
    assert report["reports"]["decisions"]["cost_usd"] == "0.00011"
    assert report["reports"]["structured_output"]["cost_usd"] is None
    assert report["reports"]["structured_output"]["unknown_cost_documents"] == 4
    assert report["reports"]["structured_output"]["known_cost_usd"] == "0.00011"
    assert report["reports"]["structured_output"]["unknown_cost_attempts"] == 11
    assert report["reports"]["structured_output"]["facet_macro_f1"] == 1
    assert (
        samples["decisions"][0].result["evaluation_orders"]
        != samples["decisions"][0].order_result["evaluation_orders"]
    )
    corpus = json.loads(DATA.read_text("utf-8"))
    assert all("rendered_text" not in case["features"] for case in corpus["cases"])
    assert all(
        sum(len(e["text"]) for e in case["features"]["excerpts"]) <= 512 for case in corpus["cases"]
    )


async def test_live_admission_caps_calls_and_retains_unknown_spend():
    ledger = EvaluationLedger(max_calls=3, budget=Decimal(".05"))
    policy = DecisionPolicy(TENANT, True, "fixture", Decimal(".016"), Decimal(".05"))
    for ordinal in range(3):
        attempt = DecisionAttempt(
            uuid4(), TENANT, ordinal + 1, "decisions", "fixture", "a" * 64, {}, "fixed", 0
        )
        await ledger.begin(attempt, policy)
        await ledger.finish(attempt, None, "decision_timeout")
    assert sum(ledger.held.values()) == Decimal(".048")
    with pytest.raises(DecisionError, match="decision_budget_exceeded"):
        await ledger.begin(
            DecisionAttempt(uuid4(), TENANT, 4, "decisions", "fixture", "a" * 64, {}, "fixed", 0),
            policy,
        )
    assert len(ledger.events) == 6


async def test_opt_in_live_flow_with_mock_transport_captures_only_validated_fields(monkeypatch):
    case = next(c for c in json.loads(DATA.read_text("utf-8"))["cases"] if c["id"] == "invoice")
    responses = case["recordings"]["decisions"]["fixed"]["responses"]
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        assert "rendered_text" not in json.loads(request.content)["state"]
        return httpx.Response(
            200, json={**responses[len(calls) - 1], "provider_extra": "private-marker"}
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        harness.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(handler)),
    )
    settings = harness.eval_settings().model_copy(
        update={"classification_tokenizer_model": harness.MODEL}
    )
    monkeypatch.setattr(harness, "eval_settings", lambda **kwargs: settings)
    monkeypatch.setattr(
        "app.ingestion.tokenizer_artifact.load_tokenizer_artifact", lambda *a, **k: "{}"
    )
    monkeypatch.setattr(
        "app.ingestion.native.classification_token_count", lambda text, artifact: len(text.split())
    )
    report = await harness.live_invoice(bins=10)
    assert len(calls) == report["calls"] == 3
    assert report["result"]["path"] == case["gold_path"]
    assert len(report["responses"]) == 3
    assert Decimal(report["held_spend_usd"]) < Decimal(".05")
    assert "synthetic-offline" not in json.dumps(report)
    assert "private-marker" not in json.dumps(report)
    assert report["metrics"]["per_level_accuracy"] == [1] * 3


def test_corpus_identity_excludes_predictions_but_includes_labels_and_evidence():
    corpus = json.loads(DATA.read_text("utf-8"))
    changed = deepcopy(corpus)
    changed["cases"][0]["recordings"] = {}
    assert harness.corpus_fingerprint(changed) == harness.corpus_fingerprint(corpus)
    changed["cases"][0]["gold_path"] = "other/other/other"
    assert harness.corpus_fingerprint(changed) != harness.corpus_fingerprint(corpus)
