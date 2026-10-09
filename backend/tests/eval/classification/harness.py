"""Recorded responses exercise the real cascade/gateway without network access."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from app.classification.taxonomy import load_taxonomy
from app.core.config import Settings
from app.domain.classification_features import ClassificationFeatures
from app.domain.decisions import DecisionPolicy
from app.domain.llm import Completion, TokenUsage
from app.llm.decisions import DecisionError, DecisionsGateway
from app.services.classification import classify
from tests.eval.classification.metrics import Sample, baseline_gate, calibration, score

DATA = Path(__file__).parent / "synthetic.json"
TENANT = UUID("00000000-0000-0000-0000-000000000694")
MODEL = "openai/gpt-6-luna-decisions"


class EvaluationLedger:
    """Serial evaluation admission; unknown outcomes retain their ceiling."""

    tenant_id = TENANT

    def __init__(self, *, max_calls: int, budget: Decimal) -> None:
        self.max_calls, self.budget = max_calls, budget
        self.held: dict[UUID, Decimal] = {}
        self.calls = 0
        self.events: list[dict[str, Any]] = []

    async def begin(self, attempt, policy):
        if (
            self.calls >= self.max_calls
            or policy.tenant_id != self.tenant_id
            or sum(self.held.values(), Decimal(0)) + policy.per_call_ceiling_usd > self.budget
        ):
            raise DecisionError("decision_budget_exceeded")
        self.calls += 1
        self.held[attempt.id] = policy.per_call_ceiling_usd
        self.events.append(
            {
                "kind": "intent",
                "id": str(attempt.id),
                "method": attempt.method,
                "model": attempt.model,
                "option_orders": dict(attempt.option_orders),
                "order_policy": attempt.order_policy,
                "order_seed": attempt.order_seed,
            }
        )

    async def finish(self, attempt, usage, error):
        if usage is not None and usage.cost_usd is not None:
            self.held[attempt.id] = usage.cost_usd
        self.events.append(
            {
                "kind": "terminal",
                "id": str(attempt.id),
                "error": error,
                "cost_usd": str(usage.cost_usd) if usage and usage.cost_usd is not None else None,
                "usage": {
                    "input_tokens": usage.tokens.prompt_tokens,
                    "output_tokens": usage.tokens.completion_tokens,
                    "cost": float(usage.cost_usd) if usage.cost_usd is not None else None,
                    "reported_model": usage.reported_model,
                }
                if usage is not None
                else None,
            }
        )


def eval_settings(*, live: bool = False, order: str = "fixed") -> Settings:
    values: dict[str, Any] = {
        "DATABASE_URL": "sqlite+aiosqlite://",
        "REDIS_URL": "redis://localhost:6379/0",
        "CELERY_BROKER_URL": "redis://localhost:6379/1",
        "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_ACCESS_KEY": "synthetic",
        "S3_SECRET_KEY": "synthetic",
        "S3_BUCKET": "synthetic",
        "ENVIRONMENT": "local",
        "DECISIONS_ENABLED": True,
        "DECISIONS_RETRIES": 0,
        "DECISIONS_CONCURRENCY": 1,
        "DECISIONS_TIMEOUT_SECONDS": 30,
        "DECISIONS_MAX_INPUT_BYTES": 32768,
        "DECISIONS_OPTION_ORDER": order,
        "DECISIONS_ORDER_SEED": 27,
    }
    if not live:
        values["OPENROUTER_API_KEY"] = "synthetic-offline"
    return Settings(_env_file=None, **values)


class Replay:
    def __init__(self, recording: dict[str, Any], mode: str):
        self.recording, self.mode = recording, mode
        self.position = 0
        self.orders = []

    def http(self, request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        self.orders.append(list(wire["questions"]["document_type"]["criteria"]))
        if self.mode == "structured_output":
            return httpx.Response(503)  # unknown primary spend must survive fallback
        response = self.recording["responses"][self.position]
        assert set(response["answers"]) == set(wire["questions"])
        assert set(response["answers"]["document_type"]["probabilities"]) == set(
            wire["questions"]["document_type"]["criteria"]
        )
        self.position += 1
        return httpx.Response(200, json=response)

    async def structured_chat(
        self, messages, *, schema, model, api_key, timeout_seconds, max_tokens
    ):
        response = self.recording["responses"][self.position]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(response["answers"])
        self.position += 1
        return Completion(
            json.dumps(response["answers"]),
            model=response["model"],
            usage=TokenUsage(40, 20, 60),
            cost_usd=Decimal(str(response["usage"]["cost"])),
        )


class NoModel:
    async def decide(self, state, questions, *, policy):
        raise DecisionError("decision_disabled")


async def replay_case(case: dict[str, Any], mode: str, order: str) -> tuple[dict[str, Any], float]:
    features = dict(case["features"])
    if mode != "rules":
        features.update(rule_path=None, rule_ids=[])
    evidence = ClassificationFeatures.from_json(json.dumps(features))
    policy = DecisionPolicy(TENANT, True, MODEL, Decimal(".005"), Decimal("1"))
    if mode == "rules":
        return await classify(
            evidence, load_taxonomy(), NoModel(), tenant_id=TENANT, policy=policy
        ), 0.0
    recording = case["recordings"][mode][order]
    replay = Replay(recording, mode)
    ledger = EvaluationLedger(max_calls=10, budget=Decimal("1"))
    if mode == "structured_output":
        policy = replace(
            policy,
            fallback_model="openrouter/openai/synthetic-fallback",
            fallback_structured_outputs=True,
        )
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay.http)) as client:
        gateway = DecisionsGateway(
            eval_settings(order=order), ledger=ledger, http_client=client, chat_gateway=replay
        )
        result = await classify(evidence, load_taxonomy(), gateway, tenant_id=TENANT, policy=policy)
    assert replay.position == len(recording["responses"]), f"unused recording: {result.get('reason')}"
    result["evaluation_orders"] = replay.orders
    return result, recording["simulated_latency_ms"]


async def offline(
    *, bins: int, data: Path = DATA
) -> tuple[dict[str, Any], dict[str, list[Sample]]]:
    raw = data.read_bytes()
    corpus = json.loads(raw)
    if corpus["taxonomy_version"] != load_taxonomy()["version"]:
        raise ValueError("taxonomy fixture mismatch")
    reports, all_samples = {}, {}
    for mode in ("rules", "decisions", "structured_output"):
        samples = []
        start = time.perf_counter()
        for case in corpus["cases"]:
            fixed, latency = await replay_case(case, mode, "fixed")
            shuffled, _ = await replay_case(case, mode, "seeded_shuffle")
            samples.append(
                Sample(
                    case["id"],
                    case["gold_path"],
                    case["gold_facets"],
                    fixed,
                    latency,
                    shuffled,
                    corpus["source"],
                    case["split"],
                )
            )
        report = score(samples, bins=bins)
        report.update(
            corpus_sha256=hashlib.sha256(raw).hexdigest(),
            taxonomy_version=corpus["taxonomy_version"],
            mode=mode,
            latency_source="scripted_fixture",
            replay_elapsed_ms=(time.perf_counter() - start) * 1000,
        )
        reports[mode], all_samples[mode] = report, samples
    return {
        "source": corpus["source"],
        "release_calibration": None,
        "reports": reports,
    }, all_samples


async def live_invoice() -> dict[str, Any]:
    """Only this bundled synthetic case may leave the process; no external corpus input."""
    from app.ingestion.native import classification_token_count
    from app.ingestion.tokenizer_artifact import load_tokenizer_artifact

    settings = eval_settings(live=True)
    if settings.classification_tokenizer_model != MODEL:
        raise ValueError("model-matched tokenizer required")
    tokenizer = load_tokenizer_artifact(
        settings.classification_tokenizer_path, sha256=settings.classification_tokenizer_sha256
    )
    case = next(
        case for case in json.loads(DATA.read_text("utf-8"))["cases"] if case["id"] == "invoice"
    )
    features = ClassificationFeatures.from_json(json.dumps(case["features"]))
    ledger = EvaluationLedger(max_calls=3, budget=Decimal(".05"))
    policy = DecisionPolicy(TENANT, True, MODEL, Decimal(".016"), Decimal(".05"))
    start = time.perf_counter()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        gateway = DecisionsGateway(
            settings,
            ledger=ledger,
            http_client=client,
            token_counter=lambda text: classification_token_count(text, tokenizer),
            max_input_tokens=8192,
        )
        result = await classify(features, load_taxonomy(), gateway, tenant_id=TENANT, policy=policy)
    # Reconstruct fixtures from validated domain values, never raw provider bodies.
    capture = []
    terminal = [event for event in ledger.events if event["kind"] == "terminal"]
    for level, event in zip(result["levels"], terminal, strict=False):
        answers = {
            "document_type": {
                "type": "choice",
                "choice": level["path"],
                "probabilities": level["probabilities"],
                "confidence": level["confidence"],
            }
        }
        if len(capture) == 2:
            answers.update(
                {
                    key: {"type": "noul", "noul": facet["probability"]}
                    for key, facet in result["facets"].items()
                }
            )
        capture.append(
            {
                "answers": answers,
                "model": event["usage"]["reported_model"],
                "usage": {
                    key: event["usage"][key] for key in ("input_tokens", "output_tokens", "cost")
                },
            }
        )
    return {
        "source": "synthetic_live",
        "case_id": "invoice",
        "taxonomy_version": "1.0.0",
        "result": result,
        "latency_ms": (time.perf_counter() - start) * 1000,
        "calls": ledger.calls,
        "held_spend_usd": str(sum(ledger.held.values(), Decimal(0))),
        "events": ledger.events,
        "responses": capture if result["status"] == "classified" else [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bins", type=int, required=True)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--tolerance", type=float)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.live:
        if args.data != DATA or args.baseline or args.approval:
            parser.error("live mode is restricted to the bundled synthetic invoice")
        report = asyncio.run(live_invoice())
    else:
        report, samples = asyncio.run(offline(bins=args.bins, data=args.data))
        if args.baseline:
            if args.tolerance is None:
                parser.error("baseline gate requires an explicit tolerance")
            previous = json.loads(args.baseline.read_text("utf-8"))
            for mode, current in report["reports"].items():
                baseline_gate(current, previous["reports"][mode], tolerance=args.tolerance)
            report["baseline_gate"] = "passed"
        if args.approval:
            config = json.loads(args.approval.read_text("utf-8"))
            if config["bins"] != args.bins:
                parser.error("bin count differs from approval")
            report["release_calibration"] = [
                calibration(
                    values,
                    config=config,
                    taxonomy_version=report["reports"][mode]["taxonomy_version"],
                    corpus_sha256=report["reports"][mode]["corpus_sha256"],
                )
                for mode, values in samples.items()
            ]
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, DecisionError):
        # No raw provider/config exceptions (which may contain secrets or content).
        raise SystemExit(
            "classification evaluation failed; check approved configuration and fixtures"
        ) from None
