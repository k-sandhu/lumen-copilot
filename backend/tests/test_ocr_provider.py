"""ADR-0029 offline schema fixtures; no provider network or tenant data."""

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.core.config import Settings
from app.domain.ocr import OcrError, OcrPage, OcrPolicy
from app.llm.ocr import OpenRouterOcrProvider

FIXTURES = Path(__file__).parent / "fixtures" / "ocr"


@pytest.mark.parametrize("fixture,status", [("success", 200), ("inference_error", 502)])
async def test_annotations_are_evidence_not_model_answer(fixture, status):
    def respond(request):
        body = json.loads(request.content)
        assert body["max_tokens"] == 1
        assert body["plugins"] == [{"id": "file-parser", "pdf": {"engine": "mistral-ocr"}}]
        file = body["messages"][0]["content"][1]["file"]
        assert file["filename"] == "page-0003.pdf"
        assert file["file_data"].startswith("data:application/pdf;base64,")
        return httpx.Response(
            status, json=json.loads((FIXTURES / (fixture + ".json")).read_text(encoding="utf-8"))
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenRouterOcrProvider(
            Settings(OPENROUTER_API_KEY="synthetic", ocr_enabled=True, ocr_model="fixture-model"),
            http_client=client,
        )
        result = await provider.recognize(OcrPage(3, b"%PDF-generated"))
    assert result.text == "Generated café 😀 e\u0301"
    assert result.engine_id == "openrouter:mistral-ocr:v1"
    assert result.confidence is None
    assert result.cost_usd == Decimal("0.00201")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": [{"message": {"content": "invented answer"}}]},
        {"choices": [{"message": {"annotations": []}}]},
    ],
)
async def test_missing_annotations_never_become_success(payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as client:
        provider = OpenRouterOcrProvider(
            Settings(OPENROUTER_API_KEY="synthetic", ocr_enabled=True, ocr_model="fixture-model"),
            http_client=client,
        )
        with pytest.raises(OcrError, match="ocr_missing_text"):
            await provider.recognize(OcrPage(1, b"%PDF-generated"))


async def test_unconfigured_never_dispatches():
    def unexpected(request):
        raise AssertionError("must not dispatch")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        provider = OpenRouterOcrProvider(
            Settings(OPENROUTER_API_KEY="", ocr_enabled=True, ocr_model="fixture-model"),
            http_client=client,
        )
        with pytest.raises(OcrError, match="ocr_unconfigured"):
            await provider.recognize(OcrPage(1, b"%PDF-generated"))


def test_policy_defaults_off_and_rejects_invalid_budgets():
    assert not OcrPolicy(uuid4()).enabled
    with pytest.raises(OcrError, match="ocr_invalid_policy"):
        OcrPolicy(uuid4(), enabled=True, page_limit=1, budget_usd=Decimal("NaN")).validate()


async def test_timeout_is_typed_and_does_not_retry():
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("private provider detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenRouterOcrProvider(
            Settings(OPENROUTER_API_KEY="synthetic", ocr_enabled=True, ocr_model="fixture-model"),
            http_client=client,
        )
        with pytest.raises(OcrError, match="ocr_timeout"):
            await provider.recognize(OcrPage(1, b"%PDF-generated"))
    assert calls == 1


async def test_response_is_bounded():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 20))
    ) as client:
        provider = OpenRouterOcrProvider(
            Settings(
                OPENROUTER_API_KEY="synthetic",
                ocr_enabled=True,
                ocr_model="fixture-model",
                ocr_max_response_bytes=10,
            ),
            http_client=client,
        )
        with pytest.raises(OcrError, match="ocr_response_budget"):
            await provider.recognize(OcrPage(1, b"%PDF-generated"))


async def test_duplicate_annotation_hash_is_deduplicated():
    payload = json.loads((FIXTURES / "success.json").read_text(encoding="utf-8"))
    payload["error"] = {
        "metadata": {"file_annotations": payload["choices"][0]["message"]["annotations"]}
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as client:
        provider = OpenRouterOcrProvider(
            Settings(OPENROUTER_API_KEY="synthetic", ocr_enabled=True, ocr_model="fixture-model"),
            http_client=client,
        )
        result = await provider.recognize(OcrPage(1, b"%PDF-generated"))
    assert result.text == "Generated café 😀 e\u0301"


@pytest.mark.parametrize("cost", ["NaN", "Infinity", -1, True, "invalid"])
async def test_malformed_cost_is_not_accepted(cost):
    payload = json.loads((FIXTURES / "success.json").read_text(encoding="utf-8"))
    payload["usage"]["cost"] = cost
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as client:
        provider = OpenRouterOcrProvider(
            Settings(OPENROUTER_API_KEY="synthetic", ocr_enabled=True, ocr_model="fixture-model"),
            http_client=client,
        )
        with pytest.raises(OcrError, match="ocr_malformed_response"):
            await provider.recognize(OcrPage(1, b"%PDF-generated"))


async def test_recorded_generated_page_recovers_without_inference_or_usage():
    payload = json.loads((FIXTURES / "recorded_generated_page.json").read_text(encoding="utf-8"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(429, json=payload))
    ) as client:
        result = await OpenRouterOcrProvider(
            Settings(OPENROUTER_API_KEY="synthetic", ocr_enabled=True, ocr_model="fixture-model"),
            http_client=client,
        ).recognize(OcrPage(1, b"%PDF-generated"))
    assert result.text == "LUMEN OCR GENERATED PAGE\n\nInvoice total 123.45\n\nReference ABC-695"
    assert result.cost_usd is None and result.input_tokens is None
    assert result.confidence is None


@pytest.mark.parametrize(
    "body", [" café 😀 e\u0301\n", '<file name="page-0001.pdf">literal</file>']
)
async def test_wrapper_removal_preserves_exact_body_codepoints(body):
    payload = json.loads((FIXTURES / "recorded_generated_page.json").read_text(encoding="utf-8"))
    payload["error"]["metadata"]["file_annotations"][0]["file"]["content"][1]["text"] = body
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(429, json=payload))
    ) as client:
        result = await OpenRouterOcrProvider(
            Settings(OPENROUTER_API_KEY="synthetic", ocr_enabled=True, ocr_model="fixture-model"),
            http_client=client,
        ).recognize(OcrPage(1, b"%PDF-generated"))
    assert result.text == body
