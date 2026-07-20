"""Offline sync harness — drives the REAL framework for any subject.

``sync_source_async`` is exercised end to end (write seam, per-page atomic
commits, cascade stamps, health counts) with only the *outside world* faked:

* object store and embedding gateway are in-memory doubles;
* the search engine is :class:`tests.acl_kit.engine.FakeEngine` behind the
  **real** :class:`~app.search.store.OpenSearchStore`, so the index side of a
  cascade stamp is proven by querying the engine rather than by counting calls;
* the connector is :class:`KitConnector`, which declares ``map_acl`` by
  delegating to the subject's real mapper and maps its documents against the
  **framework-supplied** ``run.acl_context``.

That last point is what makes these connector-agnostic: the chain proven is
"attested rows → framework snapshot → the connector's own mapper → the persisted
mirror → both chokepoints", with nothing hand-written in the middle.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from importlib import import_module
from typing import Any

from app.connectors.base import (
    AclMappingContext,
    ConnectorHealth,
    ConnectorRun,
    FetchedDoc,
    FullSyncResult,
    SourceAcl,
    SyncPage,
)
from app.core.config import Settings
from app.domain.entities import Source
from app.domain.llm import Embedding
from app.search.store import OpenSearchStore
from app.storage.keys import build_key
from app.storage.object_store import StoredObject

from .engine import FakeEngine
from .subject import AclSubject

sync_source_module = import_module("app.tasks.sync_source")

DIM = 8
_ENGINE_URL = "http://engine.invalid"
_BODY = "The quick brown fox jumps over the lazy dog. " * 6


def settings(**overrides: object) -> Settings:
    """Offline settings: no broker, no engine, tiny embeddings."""
    base: dict[str, object] = {
        "DATABASE_URL": "sqlite+aiosqlite://",
        "REDIS_URL": "redis://localhost:6379/0",
        "CELERY_BROKER_URL": "redis://localhost:6379/1",
        "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_ACCESS_KEY": "k",
        "S3_SECRET_KEY": "s",
        "S3_BUCKET": "b",
        "OPENROUTER_API_KEY": "",
        "INGESTION_CHUNK_SIZE": "120",
        "INGESTION_CHUNK_OVERLAP": "20",
        "INGESTION_EMBED_BATCH_SIZE": "3",
        "LLM_EMBEDDING_DIMENSIONS": str(DIM),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class FakeObjectStore:
    """Content-addressed in-memory object store."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(
        self, *, tenant_id: str, data: bytes, content_type: str, filename: str
    ) -> StoredObject:
        key = build_key(tenant_id, data, filename)
        self.objects[key] = data
        return StoredObject(
            key=key, sha256=key.split("/")[1], size_bytes=len(data), content_type=content_type
        )

    async def get(self, tenant_id: str, key: str) -> bytes:
        return self.objects[key]

    async def delete(self, tenant_id: str, key: str) -> None:
        self.objects.pop(key, None)


class FakeGateway:
    """Deterministic embeddings — the kit never asserts on relevance."""

    async def embed(
        self,
        inputs: Sequence[str],
        *,
        model: str | None = None,
        cache_namespace: str | None = None,
    ) -> list[Embedding]:
        return [Embedding(vector=[float(len(t) % 5)] * DIM, model="fake") for t in inputs]


def engine_bound_store_factory(engine: FakeEngine) -> type:
    """A drop-in for ``OpenSearchStore`` that binds every build to ``engine``.

    Patched over ``app.tasks.index_sync.OpenSearchStore`` so the framework's own
    index writes (ingestion, cleanup, the §3 stale-stamp update-by-query) land
    in a queryable engine instead of a call log.
    """
    import httpx

    class _EngineBoundStore(OpenSearchStore):
        @classmethod
        def from_settings(cls, _settings: Settings) -> OpenSearchStore:
            return OpenSearchStore(
                base_url=_ENGINE_URL,
                index=engine.index,
                dimensions=DIM,
                client=httpx.AsyncClient(
                    base_url=_ENGINE_URL, transport=httpx.MockTransport(engine.handler)
                ),
            )

    return _EngineBoundStore


class KitConnector:
    """A capability-declaring connector double wired to the subject's mapper.

    Declares ``map_acl`` (so the framework derives ``acl_enforced=true``
    structurally) and ``fetch_changes`` (so the §3 cascade paths are reachable).
    Documents are emitted from **raw source payloads**, mapped at emit time
    against the ``run.acl_context`` the framework froze — exactly as a real
    connector does.
    """

    def __init__(self, subject: AclSubject) -> None:
        self.subject = subject
        self.name = subject.name
        self.full_docs: tuple[tuple[str, Mapping[str, object], tuple[str, ...]], ...] = ()
        self.baseline_cursor: str | None = "cursor-1"
        self.script: dict[str, list[SyncPage | Exception]] = {}
        self.pending_pages: dict[str, list[_PageSpec]] = {}
        self.seen_context: AclMappingContext | None = None
        self.fetch_calls: list[str] = []
        self.sync_calls = 0

    # --- the framework-facing protocol ---------------------------------------

    def validate_config(self, config: dict[str, object]) -> dict[str, object]:
        return dict(config)

    async def sync(self, source: Source, run: ConnectorRun) -> FullSyncResult:
        self.sync_calls += 1
        self.seen_context = run.acl_context
        docs = tuple(
            self._fetched(external_id, raw, scopes, run)
            for external_id, raw, scopes in self.full_docs
        )
        return FullSyncResult(docs=docs, baseline_cursor=self.baseline_cursor)

    async def health(self, source: Source, run: ConnectorRun) -> ConnectorHealth:
        return ConnectorHealth(healthy=True)

    def map_acl(self, raw: Mapping[str, object], ctx: AclMappingContext) -> frozenset[str]:
        return self.subject.map_acl(raw, ctx)

    async def fetch_changes(
        self, source: Source, cursor: str, run: ConnectorRun
    ) -> AsyncIterator[SyncPage]:
        self.seen_context = run.acl_context
        self.fetch_calls.append(cursor)
        for spec in self.pending_pages.get(cursor, []):
            if isinstance(spec.raise_, Exception):
                raise spec.raise_
            yield SyncPage(
                upserts=tuple(
                    self._fetched(external_id, raw, scopes, run)
                    for external_id, raw, scopes in spec.upserts
                ),
                deleted_external_ids=spec.deleted,
                next_cursor=spec.next_cursor,
                stale_scope_ids=spec.stale_scope_ids,
                integrity=spec.integrity,
            )

    # --- helpers --------------------------------------------------------------

    def _fetched(
        self,
        external_id: str,
        raw: Mapping[str, object],
        scopes: tuple[str, ...],
        run: ConnectorRun,
    ) -> FetchedDoc:
        assert run.acl_context is not None, "the framework must supply an ACL context"
        principals = self.map_acl(raw, run.acl_context)
        return FetchedDoc(
            title=f"Doc {external_id}",
            text=f"{external_id} :: {_BODY}",
            url=f"https://source.invalid/{external_id}",
            external_id=external_id,
            acl=SourceAcl(principals=principals, scope_ids=frozenset(scopes)),
        )


class _PageSpec:
    """A scripted change page, in raw-payload terms."""

    def __init__(
        self,
        *,
        next_cursor: str,
        upserts: Sequence[tuple[str, Mapping[str, object], tuple[str, ...]]] = (),
        deleted: frozenset[str] = frozenset(),
        stale_scope_ids: frozenset[str] = frozenset(),
        integrity: Any = None,
        raise_: Exception | None = None,
    ) -> None:
        from app.connectors.base import PageIntegrity

        self.next_cursor = next_cursor
        self.upserts = tuple(upserts)
        self.deleted = deleted
        self.stale_scope_ids = stale_scope_ids
        self.integrity = integrity or PageIntegrity.COMPLETE
        self.raise_ = raise_


PageSpec = _PageSpec


async def run_sync(
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    object_store: FakeObjectStore,
    overrides: dict[str, object] | None = None,
) -> Any:
    """Run one full pass of the real sync task against the fakes."""
    return await sync_source_module.sync_source_async(
        tenant_id,
        source_id,
        settings=settings(**(overrides or {})),
        object_store=object_store,
        gateway=FakeGateway(),
    )


__all__ = [
    "DIM",
    "FakeGateway",
    "FakeObjectStore",
    "KitConnector",
    "PageSpec",
    "engine_bound_store_factory",
    "run_sync",
    "settings",
    "sync_source_module",
]
