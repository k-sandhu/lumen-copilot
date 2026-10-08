"""#690 / ADR-0027: recorded provider shapes, no inference network."""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.core.config import Settings
from app.domain.decisions import (
    ChoiceQuestion,
    DecisionOption,
    DecisionPolicy,
    PredicateQuestion,
    ScoreQuestion,
)
from app.llm.decisions import DecisionError, DecisionsGateway

FIXTURES = Path(__file__).parent / "fixtures" / "decisions"


class Ledger:
    def __init__(self, tenant_id):
        self.tenant_id = tenant_id
        self.intents = []
        self.attempts = []
        self.denied = False
        self.broken = False
        self.reserved = Decimal(0)

    async def begin(self, attempt, policy):
        if self.broken:
            raise RuntimeError("audit unavailable")
        if self.denied or self.reserved + policy.per_call_ceiling_usd > policy.budget_usd:
            raise DecisionError("decision_budget_exceeded")
        self.intents.append(attempt)
        self.reserved += policy.per_call_ceiling_usd

    async def finish(self, attempt, usage, error):
        if self.broken:
            raise RuntimeError("audit unavailable")
        self.attempts.append((attempt, usage, error))


def questions():
    return (
        ChoiceQuestion(
            "team",
            "Which team owns this?",
            (
                DecisionOption("account", "Accounts"),
                DecisionOption("frontend", "Rendering"),
                DecisionOption("payments", "Payments"),
            ),
        ),
        PredicateQuestion("is_bug", "Is this broken?"),
        ScoreQuestion("urgency", "How urgent?", ("Can wait", "Fix this week", "Blocking revenue")),
    )


def policy(tenant):
    return DecisionPolicy(
        tenant, True, "openai/gpt-6-luna-decisions", Decimal("0.01"), Decimal("1")
    )


async def run(handler, *, settings=None, selected=None, ledger=None, chat=None, qs=None):
    selected = selected or policy(uuid4())
    ledger = ledger or Ledger(selected.tenant_id)
    settings = settings or Settings(
        OPENROUTER_API_KEY="synthetic-key", DECISIONS_ENABLED=True, DECISIONS_RETRIES=0
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = DecisionsGateway(settings, ledger=ledger, http_client=client, chat_gateway=chat)
        return await gateway.decide(
            "Synthetic checkout ticket", qs or questions(), policy=selected
        ), ledger


async def test_all_three_types_and_accounting_round_trip():
    payload = json.loads((FIXTURES / "success.json").read_text("utf-8"))

    def handler(request):
        wire = json.loads(request.content)
        assert request.url == "https://openrouter.ai/api/alpha/decisions"
        assert wire["questions"]["is_bug"]["type"] == "noul"
        assert list(wire["questions"]["team"]["criteria"]) == ["account", "frontend", "payments"]
        assert wire["state"] == "Synthetic checkout ticket"
        return httpx.Response(200, json=payload)

    result, ledger = await run(handler)
    assert result.answers["team"].choice == "payments"
    assert result.answers["is_bug"].probability == 0.96
    assert result.answers["urgency"].score == 1.99
    assert result.usage.cost_usd == Decimal("0.000019992")
    assert result.model == "typesafe/jev-1.13-20260917"
    assert len(ledger.intents) == len(ledger.attempts) == 1
    assert ledger.attempts[0][2] is None


@pytest.mark.parametrize(
    "fault,code",
    [
        ("timeout", "decision_timeout"),
        ("malformed", "decision_malformed_response"),
        ("auth", "decision_provider_rejected"),
    ],
)
async def test_provider_faults_are_typed_and_never_answers(fault, code):
    tenant = uuid4()
    ledger = Ledger(tenant)

    def handler(request):
        if fault == "timeout":
            raise httpx.ReadTimeout("secret or excerpt", request=request)
        return httpx.Response(401 if fault == "auth" else 200, json={"answers": {}})

    with pytest.raises(DecisionError) as caught:
        await run(handler, selected=policy(tenant), ledger=ledger)
    assert caught.value.code == code
    assert "secret" not in str(caught.value)
    assert ledger.attempts[0][2] == code
    assert ledger.attempts[0][1] is None


@pytest.mark.parametrize(
    "change,code",
    [
        ({"OPENROUTER_API_KEY": ""}, "decision_unconfigured"),
        ({"DECISIONS_ENABLED": False}, "decision_disabled"),
    ],
)
async def test_no_dispatch_when_unconfigured_or_disabled(change, code):
    def handler(request):
        raise AssertionError("must not dispatch")

    settings = Settings(
        **{"OPENROUTER_API_KEY": "synthetic-key", "DECISIONS_ENABLED": True, **change}
    )
    with pytest.raises(DecisionError, match=code):
        await run(handler, settings=settings)


async def test_budget_audit_and_foreign_tenant_fail_before_dispatch():
    tenant = uuid4()
    ledger = Ledger(tenant)

    def handler(request):
        raise AssertionError("must not dispatch")

    ledger.denied = True
    with pytest.raises(DecisionError, match="decision_budget_exceeded"):
        await run(handler, selected=policy(tenant), ledger=ledger)
    ledger.denied = False
    ledger.broken = True
    with pytest.raises(DecisionError, match="decision_accounting_failed"):
        await run(handler, selected=policy(tenant), ledger=ledger)
    with pytest.raises(DecisionError, match="decision_tenant_mismatch"):
        await run(handler, ledger=Ledger(uuid4()))


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign_choice",
        "missing_probability",
        "nan",
        "negative_cost",
        "wrong_name",
        "refusal",
        "wrong_type",
        "fractional_tokens",
        "bad_sum",
    ],
)
async def test_invalid_recorded_answers_fail_closed(mutation):
    payload = json.loads((FIXTURES / "success.json").read_text("utf-8"))
    answer = payload["answers"]["team"]
    if mutation == "foreign_choice":
        answer["choice"] = "invented"
    if mutation == "missing_probability":
        del answer["probabilities"]["account"]
    if mutation == "nan":
        answer["confidence"] = float("nan")
    if mutation == "negative_cost":
        payload["usage"]["cost"] = -1
    if mutation == "wrong_name":
        payload["answers"]["foreign"] = payload["answers"].pop("team")
    if mutation == "refusal":
        payload["answers"]["team"] = {"type": "refusal"}
    if mutation == "wrong_type":
        answer["type"] = "score"
    if mutation == "fractional_tokens":
        payload["usage"]["input_tokens"] = 0.2
    if mutation == "bad_sum":
        answer["probabilities"]["account"] = 0.5
    with pytest.raises(DecisionError):
        await run(lambda request: httpx.Response(200, json=payload))


async def test_retries_and_fallback_are_separately_reserved_and_strict():
    from app.domain.llm import Completion, TokenUsage

    class Chat:
        async def structured_chat(
            self, messages, *, schema, model, api_key, timeout_seconds, max_tokens
        ):
            assert schema["additionalProperties"] is False
            assert schema["properties"]["team"]["properties"]["choice"]["enum"] == [
                "account",
                "frontend",
                "payments",
            ]
            assert messages[0].role.value == "system"
            assert model == "openrouter/openai/synthetic-chat"
            payload = json.loads((FIXTURES / "fallback.json").read_text("utf-8"))
            return Completion(
                content=json.dumps(payload),
                model=model,
                usage=TokenUsage(prompt_tokens=100, completion_tokens=30),
                cost_usd=Decimal("0.001"),
            )

    settings = Settings(
        OPENROUTER_API_KEY="synthetic-key",
        DECISIONS_ENABLED=True,
        DECISIONS_RETRIES=1,
        DECISIONS_RETRY_BACKOFF_SECONDS=0,
    )
    selected = replace(
        policy(uuid4()),
        fallback_model="openrouter/openai/synthetic-chat",
        fallback_structured_outputs=True,
    )
    result, ledger = await run(
        lambda request: httpx.Response(503), settings=settings, selected=selected, chat=Chat()
    )
    assert result.method == "structured_output"
    assert len(ledger.intents) == len(ledger.attempts) == 3
    assert result.total_cost_usd is None  # prior requests may have spent money
    assert ledger.attempts[0][1] is None


async def test_fallback_requires_verified_capability_and_not_for_malformed():
    selected = replace(policy(uuid4()), fallback_model="openrouter/openai/synthetic-chat")
    with pytest.raises(DecisionError, match="decision_fallback_unsupported"):
        await run(lambda request: httpx.Response(503), selected=selected)
    selected = replace(selected, fallback_structured_outputs=True)
    with pytest.raises(DecisionError, match="decision_malformed_response"):
        await run(lambda request: httpx.Response(200, json={}), selected=selected)


async def test_seeded_order_is_reproducible_and_score_order_stays_fixed():
    settings = Settings(
        OPENROUTER_API_KEY="synthetic-key",
        DECISIONS_ENABLED=True,
        DECISIONS_RETRIES=0,
        DECISIONS_OPTION_ORDER="seeded_shuffle",
        DECISIONS_ORDER_SEED=27,
    )
    seen = []

    def handler(request):
        wire = json.loads(request.content)
        seen.append(list(wire["questions"]["team"]["criteria"]))
        assert wire["questions"]["urgency"]["criteria"] == [
            "Can wait",
            "Fix this week",
            "Blocking revenue",
        ]
        return httpx.Response(200, json=json.loads((FIXTURES / "success.json").read_text("utf-8")))

    first, _ = await run(handler, settings=settings)
    second, _ = await run(handler, settings=settings)
    assert seen[0] == seen[1]
    assert first.attempts[0].option_orders["team"] == tuple(seen[0])
    assert first.attempts[0].option_orders == second.attempts[0].option_orders


@pytest.mark.parametrize(
    "state,qs",
    [
        ("", questions()),
        ("x", ()),
        ("x", (questions()[0], questions()[0])),
        (
            "x",
            (
                ChoiceQuestion(
                    "bad", "choose", (DecisionOption("a", "A"), DecisionOption("a", "B"))
                ),
            ),
        ),
    ],
)
async def test_invalid_inputs_do_not_dispatch(state, qs):
    tenant = uuid4()
    ledger = Ledger(tenant)
    settings = Settings(OPENROUTER_API_KEY="synthetic-key", DECISIONS_ENABLED=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("dispatch"))
    ) as client:
        gateway = DecisionsGateway(settings, ledger=ledger, http_client=client)
        with pytest.raises(DecisionError, match="decision_invalid_input"):
            await gateway.decide(state, qs, policy=policy(tenant))
    assert not ledger.intents


async def test_terminal_audit_failure_blocks_a_valid_answer():
    tenant = uuid4()
    ledger = Ledger(tenant)

    def handler(request):
        ledger.broken = True
        return httpx.Response(200, json=json.loads((FIXTURES / "success.json").read_text("utf-8")))

    with pytest.raises(DecisionError, match="decision_accounting_failed"):
        await run(handler, selected=policy(tenant), ledger=ledger)


async def test_cost_ceiling_records_actual_usage_but_blocks_answer():
    payload = json.loads((FIXTURES / "success.json").read_text("utf-8"))
    payload["usage"]["cost"] = 0.02
    tenant = uuid4()
    ledger = Ledger(tenant)
    with pytest.raises(DecisionError, match="decision_cost_ceiling_exceeded"):
        await run(
            lambda request: httpx.Response(200, json=payload),
            selected=policy(tenant),
            ledger=ledger,
        )
    assert ledger.attempts[0][1].cost_usd == Decimal("0.02")


async def test_deadline_and_cancellation_keep_unknown_spend():
    import asyncio

    tenant = uuid4()
    ledger = Ledger(tenant)

    async def handler(request):
        await asyncio.sleep(1)
        raise AssertionError("deadline should expire")

    settings = Settings(
        OPENROUTER_API_KEY="synthetic-key",
        DECISIONS_ENABLED=True,
        DECISIONS_TIMEOUT_SECONDS=0.01,
        DECISIONS_RETRIES=0,
    )
    with pytest.raises(DecisionError, match="decision_timeout"):
        await run(handler, settings=settings, selected=policy(tenant), ledger=ledger)
    assert ledger.attempts[0][1] is None
    assert ledger.reserved == Decimal("0.01")

    async def cancelled(request):
        raise asyncio.CancelledError()

    ledger = Ledger(tenant)
    with pytest.raises(asyncio.CancelledError):
        await run(cancelled, settings=settings, selected=policy(tenant), ledger=ledger)
    assert len(ledger.intents) == 1 and not ledger.attempts
    assert ledger.reserved == Decimal("0.01")


async def test_shared_gateway_bounds_concurrent_dispatches():
    import asyncio

    tenant = uuid4()
    ledger = Ledger(tenant)
    active = peak = 0

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json=json.loads((FIXTURES / "success.json").read_text("utf-8")))

    settings = Settings(
        OPENROUTER_API_KEY="synthetic-key", DECISIONS_ENABLED=True, DECISIONS_CONCURRENCY=1
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = DecisionsGateway(settings, ledger=ledger, http_client=client)
        await asyncio.gather(
            *(
                gateway.decide("Synthetic evidence", questions(), policy=policy(tenant))
                for _ in range(4)
            )
        )
    assert peak == 1


@pytest.mark.parametrize(
    "change",
    [
        {"DECISIONS_TIMEOUT_SECONDS": 0},
        {"DECISIONS_TIMEOUT_SECONDS": float("nan")},
        {"DECISIONS_RETRIES": -1},
        {"DECISIONS_CONCURRENCY": 0},
        {"DECISIONS_OPTION_ORDER": "arbitrary"},
        {"DECISIONS_TENANT_BUDGET_USD": -1},
        {"DECISIONS_MAX_INPUT_BYTES": 0},
    ],
)
def test_invalid_config_is_rejected(change):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(**change)


async def test_input_and_output_byte_caps_are_enforced():
    tenant = uuid4()
    ledger = Ledger(tenant)
    settings = Settings(
        OPENROUTER_API_KEY="synthetic-key", DECISIONS_ENABLED=True, DECISIONS_MAX_INPUT_BYTES=1
    )
    with pytest.raises(DecisionError, match="decision_invalid_input"):
        await run(
            lambda request: pytest.fail("dispatch"),
            settings=settings,
            selected=policy(tenant),
            ledger=ledger,
        )
    settings = Settings(
        OPENROUTER_API_KEY="synthetic-key",
        DECISIONS_ENABLED=True,
        DECISIONS_MAX_RESPONSE_BYTES=1024,
    )
    with pytest.raises(DecisionError, match="decision_malformed_response"):
        await run(lambda request: httpx.Response(200, content=b"x" * 1025), settings=settings)


async def test_fallback_schema_uses_the_recorded_shuffled_choice_order():
    from app.domain.llm import Completion, TokenUsage

    sent_order = []

    def handler(request):
        sent_order.extend(json.loads(request.content)["questions"]["team"]["criteria"])
        return httpx.Response(404)

    class Chat:
        async def structured_chat(self, messages, *, schema, **kwargs):
            assert schema["properties"]["team"]["properties"]["choice"]["enum"] == sent_order
            assert (
                list(schema["properties"]["team"]["properties"]["probabilities"]["properties"])
                == sent_order
            )
            return Completion(
                content=(FIXTURES / "fallback.json").read_text("utf-8"),
                model="synthetic",
                usage=TokenUsage(10, 2, 12),
                cost_usd=Decimal("0.001"),
            )

    settings = Settings(
        OPENROUTER_API_KEY="synthetic-key",
        DECISIONS_ENABLED=True,
        DECISIONS_RETRIES=0,
        DECISIONS_OPTION_ORDER="seeded_shuffle",
        DECISIONS_ORDER_SEED=27,
    )
    selected = replace(
        policy(uuid4()),
        fallback_model="openrouter/synthetic/chat",
        fallback_structured_outputs=True,
    )
    result, _ = await run(handler, settings=settings, selected=selected, chat=Chat())
    assert tuple(sent_order) == result.attempts[-1].option_orders["team"]
