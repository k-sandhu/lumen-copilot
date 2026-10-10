"""Readiness is activated after refreshed index synchronization (spec 0015)."""

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal
from uuid import UUID

import httpx
import pytest
import pytest_asyncio

from app.core.errors import DependencyError
from app.db.repositories import ChunkRepository, DocumentRepository
from app.db.session import tenant_session_scope
from app.domain.entities import DocumentStatus
from app.search import IndexedChunk, OpenSearchStore
from app.tasks.ingest import IngestionError, IngestionResult, ingest_document_async
from tests.test_index_sync import _FakeGateway, _FakeObjectStore, _seed, _settings
from tests.test_index_sync import sqlite_engine as sqlite_engine  # noqa: F401

pytestmark = pytest.mark.usefixtures("sqlite_engine")


class ObservedIndex:
    def __init__(self, *, fault: str | None = None) -> None:
        self.fault = fault
        self.states: list[DocumentStatus] = []
        self.refreshes: list[bool | Literal["wait_for"]] = []
        self.visible: list[IndexedChunk] = []
        self.tenant_id: UUID | None = None
        self.document_id: UUID | None = None

    async def ensure_index(self) -> None:
        pass

    async def _observe(self, refresh: bool | Literal["wait_for"], operation: str) -> None:
        assert self.tenant_id is not None and self.document_id is not None
        async with tenant_session_scope(self.tenant_id) as session:
            document = await DocumentRepository(session, self.tenant_id).get(self.document_id)
            assert document is not None
            self.states.append(document.status)
        self.refreshes.append(refresh)
        if self.fault == operation:
            raise DependencyError("synthetic synchronization failure", code="search_unavailable")

    async def delete_document_generation(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        ingestion_attempt: int,
        refresh: bool = False,
    ) -> None:
        self.tenant_id, self.document_id = tenant_id, document_id
        await self._observe(refresh, "delete-current")
        self.visible = [c for c in self.visible if c.ingestion_attempt != ingestion_attempt]

    async def delete_older_document_generations(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        ingestion_attempt: int,
        refresh: bool = False,
    ) -> None:
        self.tenant_id, self.document_id = tenant_id, document_id
        await self._observe(refresh, "delete-older")
        self.visible = [c for c in self.visible if c.ingestion_attempt >= ingestion_attempt]

    async def upsert_chunks(
        self, chunks: Sequence[IndexedChunk], *, refresh: bool | Literal["wait_for"] = False
    ) -> None:
        self.tenant_id, self.document_id = chunks[0].tenant_id, chunks[0].document_id
        await self._observe(refresh, "upsert")
        if refresh:
            self.visible = list(chunks)


async def test_ready_follows_visible_refreshed_index_writes() -> None:
    tenant_id, _, _, document_id = await _seed(chunk_texts=[])
    objects = _FakeObjectStore()
    objects.put(str(tenant_id), "k", b"Grounded native text.")
    index = ObservedIndex()
    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=_settings(),
        object_store=objects,
        gateway=_FakeGateway(),
        search_store=index,
    )
    assert index.states == [DocumentStatus.PROCESSING, DocumentStatus.PROCESSING]
    assert index.refreshes == ["wait_for", True]
    assert index.visible and result.status is DocumentStatus.READY


@pytest.mark.parametrize("fault", ["delete-older", "upsert"])
async def test_failed_synchronization_never_activates_ready_and_retry_converges(fault: str) -> None:
    tenant_id, _, _, document_id = await _seed(chunk_texts=[])
    objects = _FakeObjectStore()
    objects.put(str(tenant_id), "k", b"Grounded native text.")
    index = ObservedIndex(fault=fault)
    with pytest.raises(IngestionError, match="updating the search index"):
        await ingest_document_async(
            tenant_id,
            document_id,
            settings=_settings(),
            object_store=objects,
            gateway=_FakeGateway(),
            search_store=index,
        )
    async with tenant_session_scope(tenant_id) as session:
        document = await DocumentRepository(session, tenant_id).get(document_id)
        assert document is not None and document.status is DocumentStatus.FAILED
        assert document.ingestion_failure is not None
        assert document.ingestion_failure["code"] == "ingestion_index_error"
    index.fault = None
    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=_settings(),
        object_store=objects,
        gateway=_FakeGateway(),
        search_store=index,
    )
    assert result.status is DocumentStatus.READY and index.visible


async def test_empty_reingestion_clears_index_and_never_reports_ready() -> None:
    tenant_id, _, _, document_id = await _seed(chunk_texts=["Previously indexed text"])
    objects = _FakeObjectStore()
    objects.put(str(tenant_id), "k", b" \n ")
    index = ObservedIndex()
    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=_settings(),
        object_store=objects,
        gateway=_FakeGateway(),
        search_store=index,
    )
    assert result.status is DocumentStatus.FAILED and result.chunk_count == 0
    assert index.refreshes == [True, True]
    assert DocumentStatus.READY not in index.states
    async with tenant_session_scope(tenant_id) as session:
        assert await ChunkRepository(session, tenant_id).list_for_document(document_id) == []
        document = await DocumentRepository(session, tenant_id).get(document_id)
        assert document is not None and document.status is DocumentStatus.FAILED
        assert document.ingestion_failure is not None
        assert document.ingestion_failure["code"] == "no_native_text"
        assert "No native text" in (document.error or "")


_SHARDS = {"total": 1, "successful": 1, "failed": 0}


class HTTPIndex:
    """A deterministic engine handshake behind the production HTTP adapter.

    Only provisioning is bypassed. Writes, completion validation, refresh,
    ingestion orchestration and committed DB read-back all use production code.
    """

    def __init__(self, tenant_id: UUID, document_id: UUID) -> None:
        self.tenant_id, self.document_id = tenant_id, document_id
        self.objects = _FakeObjectStore()
        self.objects.put(str(tenant_id), "k", b"Previously searchable native text.")
        self.pending: dict[str, dict[str, Any]] = {}
        self.visible: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.states: list[DocumentStatus] = []
        self.fault: tuple[str, str] | None = None
        self.fault_hit = False

    async def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        async with tenant_session_scope(self.tenant_id) as session:
            document = await DocumentRepository(session, self.tenant_id).get(self.document_id)
            assert document is not None
            self.states.append(document.status)
        assert request.method == "POST"
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "_bulk":
            lines = [json.loads(line) for line in request.content.splitlines()]
            items = []
            for action, body in zip(lines[::2], lines[1::2], strict=True):
                assert action["index"]["routing"] == str(self.tenant_id)
                self.pending[action["index"]["_id"]] = body
                shards = _SHARDS
                if self.fault == ("bulk", "shards"):
                    self.fault_hit = True
                    shards = {"total": 2, "successful": 1, "failed": 1}
                items.append({"index": {"status": 201, "_shards": shards}})
            return httpx.Response(200, json={"errors": False, "items": items})
        if endpoint == "_refresh":
            if self.fault == ("refresh", "shards"):
                self.fault_hit = True
                # Unavailable shard, even without an explicit failed shard.
                return httpx.Response(
                    200, json={"_shards": {"total": 2, "successful": 1, "failed": 0}}
                )
            self.visible = dict(self.pending)
            return httpx.Response(200, json={"_shards": _SHARDS})
        assert endpoint == "_delete_by_query"
        filters = json.loads(request.content)["query"]["bool"]["filter"]
        assert filters[:2] == [
            {"term": {"tenant_id": str(self.tenant_id)}},
            {"term": {"document_id": str(self.document_id)}},
        ]
        assert request.url.params["routing"] == str(self.tenant_id)
        clause = filters[2]
        operation = "delete-current" if "term" in clause else "delete-older"
        response: dict[str, Any] = {
            "timed_out": False,
            "failures": [],
            "version_conflicts": 0,
        }
        if self.fault is not None and self.fault[0] == operation:
            self.fault_hit = True
            field = self.fault[1]
            response[field] = {
                "timed_out": True,
                "failures": [{"cause": "fault"}],
                "version_conflicts": 1,
            }[field]
            return httpx.Response(200, json=response)
        attempt = (
            clause["term"]["ingestion_attempt"]
            if operation == "delete-current"
            else clause["range"]["ingestion_attempt"]["lt"]
        )
        for key, body in list(self.pending.items()):
            matches = (
                body["ingestion_attempt"] == attempt
                if operation == "delete-current"
                else body["ingestion_attempt"] < attempt
            )
            if matches:
                del self.pending[key]
                if request.url.params.get("refresh") == "true":
                    self.visible.pop(key, None)
        return httpx.Response(200, json=response)

    async def ingest(self, store: OpenSearchStore) -> IngestionResult:
        return await ingest_document_async(
            self.tenant_id,
            self.document_id,
            settings=_settings(),
            object_store=self.objects,
            gateway=_FakeGateway(),
            search_store=store,
        )


@pytest_asyncio.fixture
async def http_index(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[HTTPIndex, OpenSearchStore]]:
    tenant_id, _, _, document_id = await _seed(chunk_texts=[])
    engine = HTTPIndex(tenant_id, document_id)
    settings = _settings()
    async with httpx.AsyncClient(
        base_url="http://engine.test", transport=httpx.MockTransport(engine.respond)
    ) as client:
        store = OpenSearchStore(
            base_url="http://engine.test",
            index="chunks",
            dimensions=settings.llm_embedding_dimensions,
            embedding_fingerprint=settings.embedding_space_fingerprint,
            client=client,
        )

        async def provisioned() -> None:
            pass

        monkeypatch.setattr(store, "ensure_index", provisioned)
        result = await engine.ingest(store)
        assert result.status is DocumentStatus.READY and engine.visible
        engine.requests.clear()
        engine.states.clear()
        yield engine, store


@pytest.mark.parametrize(
    "fault",
    [
        ("delete-older", "timed_out"),
        ("delete-older", "failures"),
        ("delete-older", "version_conflicts"),
        ("bulk", "shards"),
        ("refresh", "shards"),
    ],
)
async def test_incomplete_http_200_never_activates_ready_and_retry_converges(
    http_index: tuple[HTTPIndex, OpenSearchStore], fault: tuple[str, str]
) -> None:
    engine, store = http_index
    engine.fault = fault
    engine.objects.put(str(engine.tenant_id), "k", b"Replacement native text.")
    with pytest.raises(IngestionError) as raised:
        await engine.ingest(store)
    assert raised.value.code == "ingestion_index_error"
    assert engine.fault_hit and set(engine.states) == {DocumentStatus.PROCESSING}
    async with tenant_session_scope(engine.tenant_id) as session:
        document = await DocumentRepository(session, engine.tenant_id).get(engine.document_id)
        assert document is not None and document.status is DocumentStatus.FAILED
    engine.fault = None
    result = await engine.ingest(store)
    assert result.status is DocumentStatus.READY
    assert len(engine.visible) == 1
    assert next(iter(engine.visible.values()))["text"] == "Replacement native text."


@pytest.mark.parametrize("operation", ["delete-current", "delete-older"])
@pytest.mark.parametrize("field", ["timed_out", "failures", "version_conflicts"])
async def test_empty_cleanup_incomplete_http_200_is_retryable(
    http_index: tuple[HTTPIndex, OpenSearchStore], operation: str, field: str
) -> None:
    engine, store = http_index
    engine.objects.put(str(engine.tenant_id), "k", b" \n ")
    engine.fault = (operation, field)
    with pytest.raises(IngestionError) as raised:
        await engine.ingest(store)
    assert raised.value.code == "ingestion_index_error"
    assert engine.fault_hit and set(engine.states) == {DocumentStatus.PROCESSING}
    async with tenant_session_scope(engine.tenant_id) as session:
        document = await DocumentRepository(session, engine.tenant_id).get(engine.document_id)
        assert document is not None and document.status is DocumentStatus.FAILED
        assert (
            await ChunkRepository(session, engine.tenant_id).list_for_document(engine.document_id)
            == []
        )
    engine.fault = None
    result = await engine.ingest(store)
    assert result.status is DocumentStatus.FAILED
    assert "No native text" in (result.error or "")
    assert engine.visible == {}


async def test_empty_reingestion_refreshes_real_adapter_cleanup_before_failure(
    http_index: tuple[HTTPIndex, OpenSearchStore],
) -> None:
    engine, store = http_index
    engine.objects.put(str(engine.tenant_id), "k", b" \n ")
    result = await engine.ingest(store)
    assert result.status is DocumentStatus.FAILED and result.chunk_count == 0
    assert engine.pending == engine.visible == {}
    assert engine.states == [DocumentStatus.PROCESSING, DocumentStatus.PROCESSING]
    assert all(request.url.params.get("refresh") == "true" for request in engine.requests)
    async with tenant_session_scope(engine.tenant_id) as session:
        document = await DocumentRepository(session, engine.tenant_id).get(engine.document_id)
        assert document is not None and document.status is DocumentStatus.FAILED
        assert document.ingestion_failure is not None
        assert document.ingestion_failure["code"] == "no_native_text"


async def test_empty_finalization_cannot_fail_a_newer_ready_attempt() -> None:
    tenant_id, _, _, document_id = await _seed(chunk_texts=["Prior text"])
    objects = _FakeObjectStore()
    objects.put(str(tenant_id), "k", b" \n ")
    newer_index = ObservedIndex()

    class TakeoverIndex(ObservedIndex):
        async def delete_older_document_generations(
            self,
            *,
            tenant_id: UUID,
            document_id: UUID,
            ingestion_attempt: int,
            refresh: bool = False,
        ) -> None:
            await super().delete_older_document_generations(
                tenant_id=tenant_id,
                document_id=document_id,
                ingestion_attempt=ingestion_attempt,
                refresh=refresh,
            )
            # Deterministic handoff at the cleanup/finalization boundary. A
            # recovery delivery completes a new attempt before the old resumes.
            async with tenant_session_scope(tenant_id) as session:
                failed = await DocumentRepository(session, tenant_id).mark_ingestion_failed(
                    document_id,
                    expected_attempt=ingestion_attempt,
                    code="test_handoff",
                    message="Recovered by another delivery.",
                )
                assert failed is not None
            objects.put(str(tenant_id), "k", b"Newer searchable text.")
            newer = await ingest_document_async(
                tenant_id,
                document_id,
                settings=_settings(),
                object_store=objects,
                gateway=_FakeGateway(),
                search_store=newer_index,
            )
            assert newer.status is DocumentStatus.READY

    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=_settings(),
        object_store=objects,
        gateway=_FakeGateway(),
        search_store=TakeoverIndex(),
    )
    assert result.status is DocumentStatus.READY and result.chunk_count == 1
    async with tenant_session_scope(tenant_id) as session:
        document = await DocumentRepository(session, tenant_id).get(document_id)
        assert document is not None and document.status is DocumentStatus.READY
        assert document.ingestion_attempts == 2
        assert document.error is None and document.ingestion_failure is None
        chunks = await ChunkRepository(session, tenant_id).list_for_document(document_id)
        assert [chunk.text for chunk in chunks] == ["Newer searchable text."]
    assert [chunk.text for chunk in newer_index.visible] == ["Newer searchable text."]
