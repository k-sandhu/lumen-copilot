"""Offline context/evidence separation, budget and fingerprint regressions."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.config import Settings
from app.domain.llm import Completion, TokenUsage


@pytest.mark.asyncio
async def test_context_is_separate_cached_and_budgeted() -> None:
    from app.domain.chunk_enrichment import ContextInput
    from app.services.chunk_enrichment import enrich_chunks

    class Cache:
        values: dict[str, str] = {}

        async def get(self, fingerprint: str) -> str | None:
            return self.values.get(fingerprint)

        async def put(self, fingerprint: str, text: str) -> None:
            self.values[fingerprint] = text

        async def claim(
            self, fingerprint: str, reserved_tokens: int, budget_fingerprint: str
        ) -> bool:
            assert reserved_tokens > 0
            if fingerprint in self.values:
                return False
            self.values[fingerprint] = ""
            return True

    class Gateway:
        calls = 0

        async def chat(self, *args: object, **kwargs: object) -> Completion:
            self.calls += 1
            assert kwargs["max_tokens"] == 64
            return Completion("GeneratedSentinel", "fixture-model", usage=TokenUsage(10, 3, 13))

    gateway, cache = Gateway(), Cache()
    settings = Settings.model_construct(
        chunk_context_generated_enabled=True,
        chunk_context_model="fixture-model",
        chunk_context_output_tokens=64,
        chunk_context_max_calls=1,
    )
    chunk = ContextInput(
        "Evidence", 0, 8, "Heading: Finance\nHeader: Revenue\nUnit: USD", "b1", (0,)
    )
    tenant, document = uuid4(), uuid4()
    args = {
        "source": "Evidence",
        "chunks": (chunk,),
        "title": "SuppliedTitle",
        "document_type": "report",
        "source_format": "text/plain",
        "source_fingerprint": "source-v1",
        "tenant_id": tenant,
        "document_id": document,
        "settings": settings,
        "gateway": gateway,
        "cache": cache,
    }
    first = (await enrich_chunks(**args))[0]
    assert "SuppliedTitle" in first.deterministic_text
    assert "Finance" in first.deterministic_text and "USD" in first.deterministic_text
    assert first.generated_text == "GeneratedSentinel"
    assert first.origin == "generated"
    assert first.block_id == "b1" and first.cell_indices == (0,)
    assert chunk.text == "Evidence" and chunk.char_start == 0 and chunk.char_end == 8
    assert (await enrich_chunks(**args))[0] == first
    assert gateway.calls == 1
    assert (await enrich_chunks(**(args | {"source_fingerprint": "source-v2"})))[
        0
    ].fingerprint != first.fingerprint
    assert gateway.calls == 2
    assert (await enrich_chunks(**(args | {"tenant_id": uuid4()})))[
        0
    ].fingerprint != first.fingerprint
    assert gateway.calls == 3


@pytest.mark.asyncio
async def test_disabled_generation_and_wrong_evidence_fail_closed() -> None:
    from app.domain.chunk_enrichment import ContextInput
    from app.services.chunk_enrichment import enrich_chunks

    args = {
        "source": "Evidence",
        "chunks": (ContextInput("Evidence", 0, 8),),
        "title": "Title",
        "document_type": None,
        "source_format": "text/plain",
        "source_fingerprint": "v1",
        "tenant_id": uuid4(),
        "document_id": uuid4(),
        "settings": Settings(),
        "gateway": None,
        "cache": None,
    }
    context = (await enrich_chunks(**args))[0]
    assert context.generated_text is None
    assert "Document type: unknown" in context.deterministic_text
    with pytest.raises(ValueError, match="evidence"):
        await enrich_chunks(**(args | {"source": "Different"}))


# The fixture supplies an isolated SQLite transaction boundary; no live datastore.
from tests.test_ingestion_task import sqlite_engine  # noqa: E402, F401, F811


@pytest.mark.asyncio
async def test_citations_hydrate_evidence_only(sqlite_engine):  # noqa: F811
    import app.db.session as db_session
    from app.db.repositories import (
        ChatSessionRepository,
        ChunkInput,
        ChunkRepository,
        CitationRepository,
        DocumentRepository,
        MessageRepository,
    )
    from app.domain.entities import MessageRole
    from tests.test_ingestion_task import _seed_document

    tenant, doc = await _seed_document(mime_type="text/plain", key="fixture")
    async with db_session.session_scope() as session:
        document = await DocumentRepository(session, tenant).get(doc)
        chunks = await ChunkRepository(session, tenant).replace_for_document(
            doc,
            [
                ChunkInput(
                    text="Evidence",
                    char_start=0,
                    char_end=8,
                    context_text="ContextSentinel",
                    generated_context="GeneratedSentinel",
                    context_metadata={"generated_origin": "model_generated"},
                )
            ],
        )
        chat = await ChatSessionRepository(session, tenant).create(
            owner_id=document.owner_id, model="fixture"
        )
        message = await MessageRepository(session, tenant).add(
            session_id=chat.id, role=MessageRole.ASSISTANT, content="answer"
        )
        repo = CitationRepository(session, tenant)
        await repo.add(message_id=message.id, chunk_id=chunks[0].id, char_start=0, char_end=8)
        hydrated = await repo.list_for_message_hydrated(message.id)
        assert hydrated[0].snippet == "Evidence"
        assert "Sentinel" not in hydrated[0].snippet
        assert chunks[0].context_text == "ContextSentinel"


@pytest.mark.asyncio
async def test_index_fields_are_separate():
    import json
    from dataclasses import replace

    import httpx

    from app.search.store import _index_body
    from tests.test_search_store import _bulk_success, _chunk, _store

    seen = []

    def handler(request):
        seen.extend(request.content.decode().strip().split("\n"))
        return httpx.Response(200, json=_bulk_success(request))

    store = _store(httpx.MockTransport(handler))
    chunk = replace(
        _chunk(tenant_id=uuid4(), owner_id=uuid4(), text="Evidence"),
        context_text="ContextSentinel",
        generated_context="GeneratedSentinel",
        context_metadata={"generated_origin": "model_generated"},
    )
    await store.upsert_chunks([chunk])
    await store.aclose()
    body = json.loads(seen[1])
    assert body["text"] == "Evidence"
    assert body["context_text"] == "ContextSentinel"
    assert body["generated_context"] == "GeneratedSentinel"
    properties = _index_body(8, "f" * 64)["mappings"]["properties"]
    assert properties["context_text"]["type"] == "text"
    assert properties["generated_context"]["type"] == "text"


@pytest.mark.parametrize("failure", ["provider", "oversized", "length", "usage"])
@pytest.mark.asyncio
async def test_invalid_generation_consumes_claim_without_mutating_evidence(failure):
    from app.core.errors import DependencyError
    from app.domain.chunk_enrichment import ContextInput
    from app.services.chunk_enrichment import enrich_chunks

    class Cache:
        def __init__(self):
            self.values = {}

        async def get(self, fp):
            return self.values.get(fp)

        async def claim(self, fp, tokens, budget_fp):
            self.values[fp] = ""
            return True

        async def put(self, fp, text):
            self.values[fp] = text

    class Gateway:
        calls = 0

        async def chat(self, *args, **kwargs):
            self.calls += 1
            if failure == "provider":
                raise DependencyError("fixture", code="fixture")
            return Completion(
                "x" * (5000 if failure == "oversized" else 5),
                "fixture",
                finish_reason="length" if failure == "length" else "stop",
                usage=TokenUsage(
                    1, 9999 if failure == "usage" else 1, 10000 if failure == "usage" else 2
                ),
            )

    gateway, cache = Gateway(), Cache()
    args = {
        "source": "Evidence",
        "chunks": (ContextInput("Evidence", 0, 8),),
        "title": "Title",
        "document_type": None,
        "source_format": "text/plain",
        "source_fingerprint": "fixture",
        "tenant_id": uuid4(),
        "document_id": uuid4(),
        "gateway": gateway,
        "cache": cache,
        "settings": Settings.model_construct(
            chunk_context_generated_enabled=True, chunk_context_model="fixture"
        ),
    }
    assert (await enrich_chunks(**args))[0].generated_text is None
    assert (await enrich_chunks(**args))[0].generated_text is None
    assert gateway.calls == 1
    args["settings"] = Settings.model_construct(
        chunk_context_generated_enabled=True,
        chunk_context_model="fixture",
        chunk_context_max_tokens=1,
    )
    assert (await enrich_chunks(**args))[0].generated_text is None
    assert gateway.calls == 1


def test_native_structure_lineage_remains_separate():
    import json

    from app.domain.chunk_enrichment import native_context_inputs
    from app.domain.native_chunking import ChunkedDocument

    raw = {
        "rendered": {
            "document": {
                "schema_version": 1,
                "renderer_version": 1,
                "blocks": [],
                "source_parts": [],
                "generation": {},
            },
            "rendered_text": "Evidence",
            "spans": [],
        },
        "chunks": [
            {
                "ord": 0,
                "text": "Evidence",
                "char_start": 0,
                "char_end": 8,
                "block_id": "b1",
                "context": "Heading: Finance\nHeader: Amount\nUnit: USD",
                "context_cell_indices": [0],
                "token_count": 1,
            }
        ],
    }
    inputs = native_context_inputs(ChunkedDocument.from_json(json.dumps(raw)))
    assert inputs[0].text == "Evidence"
    assert inputs[0].block_id == "b1" and inputs[0].cell_indices == (0,)
    assert "USD" in inputs[0].structural_context


def test_operational_generation_requires_contract_review():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="reviewed audit contract"):
        Settings(chunk_context_generated_enabled=True, chunk_context_model="fixture")
