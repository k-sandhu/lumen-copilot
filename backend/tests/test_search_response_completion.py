"""PR605 R7: HTTP 200 is not an OpenSearch completion acknowledgement."""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from app.core.config import get_settings
from app.core.errors import DependencyError
from app.db import session as db_session
from app.db.repositories import ChunkRepository, DocumentRepository, SourceRepository
from app.domain.entities import DocumentStatus
from app.domain.llm import Embedding
from app.search import OpenSearchStore, SearchAllowFilter
from app.tasks.ingest import IngestionError, ingest_document_async
from tests.test_embedding_source_recovery import _seed
from tests.test_embedding_source_recovery import source_db as _source_db
from tests.test_index_sync import _FakeObjectStore
from tests.test_search_store import _DIMS, _FINGERPRINT, _SHARDS, _bulk_success, _chunk, _store

source_db = _source_db


_BAD_SHARDS = [
    pytest.param({"total": 1, "successful": 0, "failed": 0}, id="unavailable"),
    pytest.param({"total": 2, "successful": 1, "failed": 0}, id="partial-no-failure"),
    pytest.param({"total": 2, "successful": 1, "failed": 1}, id="failed"),
    pytest.param({"total": 0, "successful": 0, "failed": 0}, id="zero-targets"),
    pytest.param({"failed": 0}, id="missing-counts"),
    pytest.param({"total": True, "successful": True, "failed": False}, id="bool-counts"),
    pytest.param({"total": 1, "successful": 2, "failed": 0}, id="impossible-counts"),
    pytest.param({"total": 1, "successful": "1", "failed": 0}, id="string-count"),
    pytest.param({"total": -1, "successful": -1, "failed": 0}, id="negative-count"),
    pytest.param(None, id="null-shards"),
]


@pytest.mark.parametrize("shards", _BAD_SHARDS)
async def test_refresh_requires_complete_positive_shard_ack(shards: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _bulk_success(request) if request.url.path == "/_bulk" else {"_shards": shards}
        return httpx.Response(200, json=payload)

    store = _store(httpx.MockTransport(handler))
    try:
        with pytest.raises(DependencyError) as failure:
            await store.upsert_chunks(
                [_chunk(tenant_id=uuid4(), owner_id=uuid4())], refresh="wait_for"
            )
        assert failure.value.code == "search_error"
    finally:
        await store.aclose()


@pytest.mark.parametrize(
    "fault",
    [
        "errors",
        "item-status",
        "item-error",
        "item-shards",
        "missing-item-shards",
        "missing-items",
        "empty-items",
        "extra-items",
        "missing-errors",
        "malformed-item",
    ],
)
async def test_bulk_requires_every_item_to_succeed(fault: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _bulk_success(request)
        item = payload["items"][0]["index"]
        if fault == "errors":
            payload["errors"] = True
        elif fault == "item-status":
            item["status"] = 500
        elif fault == "item-error":
            item["error"] = {"reason": "secret document content"}
        elif fault == "item-shards":
            item["_shards"] = {"total": 1, "successful": 0, "failed": 0}
        elif fault == "missing-item-shards":
            item.pop("_shards")
        elif fault == "missing-items":
            payload.pop("items")
        elif fault == "empty-items":
            payload["items"] = []
        elif fault == "extra-items":
            payload["items"].append(copy.deepcopy(payload["items"][0]))
        elif fault == "missing-errors":
            payload.pop("errors")
        else:
            payload["items"] = [None]
        return httpx.Response(200, json=payload)

    store = _store(httpx.MockTransport(handler))
    try:
        with pytest.raises(DependencyError) as failure:
            await store.upsert_chunks([_chunk(tenant_id=uuid4(), owner_id=uuid4())])
        assert failure.value.code == "search_index_error"
        assert "secret" not in str(failure.value)
    finally:
        await store.aclose()


@pytest.mark.parametrize("operation", ["delete", "generation", "older", "stale", "fresh"])
@pytest.mark.parametrize("fault", ["timeout", "failures", "conflicts", "shards"])
async def test_by_query_partial_completion_is_rejected(operation: str, fault: str) -> None:
    payload: dict[str, Any] = {"timed_out": False, "failures": [], "version_conflicts": 0}
    payload.update(
        {
            "timeout": {"timed_out": True},
            "failures": {"failures": [{"reason": "secret document content"}]},
            "conflicts": {"version_conflicts": 1},
            "shards": {"_shards": {"total": 1, "successful": 0, "failed": 0}},
        }[fault]
    )
    store = _store(httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    tenant, document = uuid4(), uuid4()
    try:
        with pytest.raises(DependencyError) as failure:
            if operation == "delete":
                await store.delete_document(tenant_id=tenant, document_id=document)
            elif operation == "generation":
                await store.delete_document_generation(
                    tenant_id=tenant, document_id=document, ingestion_attempt=3
                )
            elif operation == "older":
                await store.delete_older_document_generations(
                    tenant_id=tenant, document_id=document, ingestion_attempt=3
                )
            elif operation == "stale":
                await store.stamp_acl_stale(tenant_id=tenant, document_ids=[document])
            else:
                await store.attest_acl_fresh(
                    tenant_id=tenant, document_ids=[document], synced_at=datetime.now(UTC)
                )
        assert failure.value.code == "search_error"
        assert "secret" not in str(failure.value)
    finally:
        await store.aclose()


@pytest.mark.parametrize("stage", ["create", "mapping", "pipeline"])
@pytest.mark.parametrize("fault", ["unacknowledged", "missing-ack", "partial-shards"])
async def test_schema_completion_is_required_before_latching(stage: str, fault: str) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "HEAD":
            return httpx.Response(404 if stage == "create" else 200)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "lumen-test": {
                        "mappings": {
                            "_meta": {"lumen_embedding_space": _FINGERPRINT},
                            "properties": {"embedding": {"dimension": _DIMS}},
                        }
                    }
                },
            )
        payload: dict[str, Any] = {"acknowledged": True}
        if path == "/lumen-test":
            payload["shards_acknowledged"] = True
        target = {
            "create": "/lumen-test",
            "mapping": "/lumen-test/_mapping",
            "pipeline": "/_search/pipeline/lumen-test-hybrid",
        }[stage]
        if path == target:
            if fault == "unacknowledged":
                payload["acknowledged"] = False
            elif fault == "missing-ack":
                payload.pop("acknowledged")
            else:
                payload["_shards"] = {"total": 1, "successful": 0, "failed": 0}
        return httpx.Response(200, json=payload)

    store = _store(httpx.MockTransport(handler))
    try:
        for _ in range(2):
            with pytest.raises(DependencyError):
                await store.ensure_index()
        assert len([call for call in calls if call[0] == "HEAD"]) == 2
        assert not store._ensured
    finally:
        await store.aclose()


@pytest.mark.parametrize("shards_ack", [False, None, "true"])
async def test_index_creation_requires_shards_acknowledged(shards_ack: object) -> None:
    payload = {"acknowledged": True, "shards_acknowledged": shards_ack}
    store = _store(httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    try:
        with pytest.raises(DependencyError):
            await store._request("PUT", "/lumen-test", json_body={})
    finally:
        await store.aclose()


@pytest.mark.parametrize("operation", ["aliases", "alias-put", "alias-delete", "count", "search"])
@pytest.mark.parametrize("fault", ["shards", "timeout", "errors", "failures", "ack"])
async def test_other_response_kinds_share_completion_validation(operation: str, fault: str) -> None:
    payload: dict[str, Any] = {
        "acknowledged": True,
        "_shards": dict(_SHARDS),
        "timed_out": False,
        "count": 0,
        "hits": {"hits": []},
    }
    payload.update(
        {
            "shards": {"_shards": {"total": 2, "successful": 1, "failed": 0}},
            "timeout": {"timed_out": True},
            "errors": {"errors": True},
            "failures": {"failures": [{"reason": "secret document content"}]},
            "ack": {"acknowledged": False},
        }[fault]
    )
    store = _store(httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    try:
        with pytest.raises(DependencyError) as failure:
            if operation == "search":
                await store.hybrid_search(
                    query_text="budget",
                    embedding=[0.0] * _DIMS,
                    allow=SearchAllowFilter(tenant_id=uuid4(), owner_ids=frozenset({uuid4()})),
                    k=5,
                )
            else:
                method, path = {
                    "aliases": ("POST", "/_aliases"),
                    "alias-put": ("PUT", "/lumen-test/_alias/current"),
                    "alias-delete": ("DELETE", "/lumen-test/_alias/current"),
                    "count": ("GET", "/lumen-test/_count"),
                }[operation]
                await store._request(method, path)
        assert failure.value.code == "search_error"
        assert "secret" not in str(failure.value)
    finally:
        await store.aclose()


@pytest.mark.parametrize(
    "operation",
    [
        "refresh",
        "bulk",
        "delete",
        "update",
        "create",
        "mapping",
        "pipeline",
        "aliases",
        "count",
        "search",
    ],
)
async def test_complete_response_positive_controls(operation: str) -> None:
    payloads: dict[str, tuple[str, str, dict[str, Any]]] = {
        "refresh": ("POST", "/lumen-test/_refresh", {"_shards": dict(_SHARDS)}),
        "bulk": (
            "POST",
            "/_bulk",
            {"errors": False, "items": [{"index": {"status": 201, "_shards": dict(_SHARDS)}}]},
        ),
        "delete": (
            "POST",
            "/lumen-test/_delete_by_query",
            {"timed_out": False, "failures": [], "version_conflicts": 0},
        ),
        "update": (
            "POST",
            "/lumen-test/_update_by_query",
            {"timed_out": False, "failures": [], "version_conflicts": 0},
        ),
        "create": ("PUT", "/lumen-test", {"acknowledged": True, "shards_acknowledged": True}),
        "mapping": ("PUT", "/lumen-test/_mapping", {"acknowledged": True}),
        "pipeline": ("PUT", "/_search/pipeline/lumen-test-hybrid", {"acknowledged": True}),
        "aliases": ("POST", "/_aliases", {"acknowledged": True}),
        "count": ("GET", "/lumen-test/_count", {"count": 0, "_shards": dict(_SHARDS)}),
        "search": (
            "POST",
            "/lumen-test/_search",
            {"timed_out": False, "_shards": dict(_SHARDS), "hits": {"hits": []}},
        ),
    }
    method, path, payload = payloads[operation]
    store = _store(httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    try:
        assert await store._request(method, path) == payload
    finally:
        await store.aclose()


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("POST", "/lumen-test/_refresh", {}),
        ("POST", "/lumen-test/_search", {"hits": {"hits": []}, "timed_out": False}),
        ("GET", "/lumen-test/_count", {"count": 0}),
        ("POST", "/lumen-test/_delete_by_query", {"failures": []}),
        ("POST", "/lumen-test/_update_by_query", {"timed_out": False}),
        ("POST", "/_aliases", {}),
        ("POST", "/lumen-test/_search", {"hits": {"hits": []}, "_shards": _SHARDS}),
        (
            "POST",
            "/lumen-test/_search",
            {
                "hits": {"hits": []},
                "_shards": _SHARDS,
                "timed_out": False,
                "terminated_early": True,
            },
        ),
    ],
)
async def test_missing_or_truncated_completion_is_rejected(
    method: str,
    path: str,
    payload: dict[str, Any],
) -> None:
    store = _store(httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    try:
        with pytest.raises(DependencyError):
            await store._request(method, path)
    finally:
        await store.aclose()


@pytest.mark.parametrize("content", [b"not JSON: secret document content", b"[]", b"null"])
async def test_malformed_response_is_a_safe_typed_failure(content: bytes) -> None:
    store = _store(httpx.MockTransport(lambda _: httpx.Response(200, content=content)))
    try:
        with pytest.raises(DependencyError) as failure:
            await store._request("POST", "/lumen-test/_refresh")
        assert "secret" not in str(failure.value)
    finally:
        await store.aclose()


@pytest.mark.parametrize("exists", [False, True])
async def test_only_verified_index_exists_race_is_accepted(exists: bool) -> None:
    mutations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(404)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "lumen-test": {
                        "mappings": {
                            "_meta": {"lumen_embedding_space": _FINGERPRINT},
                            "properties": {"embedding": {"dimension": _DIMS}},
                        }
                    }
                },
            )
        mutations.append(request.url.path)
        if request.url.path == "/lumen-test":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "type": "resource_already_exists_exception"
                        if exists
                        else "illegal_argument_exception",
                        "reason": "secret document content",
                    }
                },
            )
        return httpx.Response(200, json={"acknowledged": True})

    store = _store(httpx.MockTransport(handler))
    try:
        if exists:
            await store.ensure_index()
            assert store._ensured
        else:
            with pytest.raises(DependencyError) as failure:
                await store.ensure_index()
            assert "secret" not in str(failure.value)
            assert not store._ensured
            assert mutations == ["/lumen-test"]
    finally:
        await store.aclose()


@pytest.mark.parametrize(
    "refresh_shards",
    [
        pytest.param({"total": 1, "successful": 0, "failed": 0}, id="unavailable-shard"),
        pytest.param({"total": 2, "successful": 1, "failed": 0}, id="partial-shards"),
        pytest.param(dict(_SHARDS), id="complete"),
    ],
)
async def test_real_ingestion_requires_refresh_completion(
    source_db: bool, refresh_shards: dict[str, int]
) -> None:
    """Actual repositories/Ready CAS: an incomplete 200 must finalize Failed."""
    tenant, source_id = await _seed()
    settings = get_settings()
    async with db_session.tenant_session_scope(tenant) as session:
        source = await SourceRepository(session, tenant).get(source_id)
        assert source is not None
        document = await DocumentRepository(session, tenant).create(
            owner_id=source.owner_id,
            collection_id=UUID(source.config["collection_id"]),
            filename="ack.txt",
            mime_type="text/plain",
            size_bytes=24,
            storage_key="ack-key",
            acl_enforced=False,
        )
    objects = _FakeObjectStore()
    objects.put(str(tenant), document.storage_key, b"native embedding refresh passage")
    complete = refresh_shards["successful"] == refresh_shards["total"]
    staged: dict[str, dict[str, Any]] = {}
    searchable: dict[str, dict[str, Any]] = {}
    bulk_calls = 0
    search_calls = 0
    cleanup_attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal bulk_calls, search_calls
        path = request.url.path
        if request.method == "HEAD":
            return httpx.Response(200)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    settings.opensearch_index: {
                        "mappings": {
                            "_meta": {
                                "lumen_embedding_space": settings.embedding_space_fingerprint
                            },
                            "properties": {
                                "embedding": {"dimension": settings.llm_embedding_dimensions}
                            },
                        }
                    }
                },
            )
        if request.method == "PUT":
            return httpx.Response(200, json={"acknowledged": True})
        if path == "/_bulk":
            bulk_calls += 1
            lines = request.content.decode().strip().split("\n")
            for action, body in zip(lines[::2], lines[1::2], strict=True):
                staged[json.loads(action)["index"]["_id"]] = json.loads(body)
            return httpx.Response(200, json=_bulk_success(request))
        if path.endswith("/_refresh"):
            if complete:
                searchable.update(staged)
            return httpx.Response(200, json={"_shards": refresh_shards})
        if path.endswith("/_search"):
            search_calls += 1
            return httpx.Response(
                200,
                json={
                    "timed_out": False,
                    "_shards": dict(_SHARDS),
                    "hits": {
                        "hits": [{"_source": body, "_score": 1.0} for body in searchable.values()]
                    },
                },
            )
        assert path.endswith("/_delete_by_query")
        filters = json.loads(request.content)["query"]["bool"]["filter"]
        for clause in filters:
            if "ingestion_attempt" in clause.get("term", {}):
                cleanup_attempts.append(clause["term"]["ingestion_attempt"])
                staged.clear()
                searchable.clear()
        return httpx.Response(
            200,
            json={
                "timed_out": False,
                "failures": [],
                "version_conflicts": 0,
            },
        )

    class _Gateway:
        async def embed(self, inputs: Sequence[str], **kwargs: object) -> list[Embedding]:
            return [
                Embedding(vector=[0.125] * settings.llm_embedding_dimensions, model="native-fake")
                for _ in inputs
            ]

    store = OpenSearchStore(
        base_url="http://opensearch.test",
        index=settings.opensearch_index,
        dimensions=settings.llm_embedding_dimensions,
        embedding_fingerprint=settings.embedding_space_fingerprint,
        client=httpx.AsyncClient(
            base_url="http://opensearch.test", transport=httpx.MockTransport(handler)
        ),
    )
    try:
        if complete:
            outcome = await ingest_document_async(
                tenant,
                document.id,
                settings=settings,
                object_store=objects,
                gateway=_Gateway(),
                search_store=store,
            )
            assert outcome.status is DocumentStatus.READY
        else:
            with pytest.raises(IngestionError) as failure:
                await ingest_document_async(
                    tenant,
                    document.id,
                    settings=settings,
                    object_store=objects,
                    gateway=_Gateway(),
                    search_store=store,
                )
            assert failure.value.code == "ingestion_index_error"
            assert cleanup_attempts == [1]
        async with db_session.tenant_session_scope(tenant) as session:
            persisted = await DocumentRepository(session, tenant).get(document.id)
            chunks = await ChunkRepository(session, tenant).list_for_document(document.id)
        assert persisted is not None and persisted.ingestion_attempts == 1
        assert persisted.status is (DocumentStatus.READY if complete else DocumentStatus.FAILED)
        assert len(chunks) > 0
        assert bulk_calls == 1
        if complete:
            assert persisted.error is None
        else:
            assert persisted.ingestion_failure is not None
            assert persisted.ingestion_failure["code"] == "ingestion_index_error"
            assert persisted.ingestion_failure["attempt"] == 1
            assert "secret" not in str(persisted.ingestion_failure)
            assert not searchable
        hits = await store.hybrid_search(
            query_text="refresh passage",
            embedding=[0.125] * settings.llm_embedding_dimensions,
            allow=SearchAllowFilter(tenant_id=tenant, owner_ids=frozenset({source.owner_id})),
            k=5,
        )
        assert {hit.chunk_id for hit in hits} == (
            {chunk.id for chunk in chunks} if complete else set()
        )
        assert all(hit.ingestion_attempt == 1 for hit in hits)
        assert search_calls == 1
    finally:
        await store.aclose()
