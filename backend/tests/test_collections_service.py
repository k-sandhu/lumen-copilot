"""Collections service unit tests — the cursor codec (#46) + object cleanup (#269).

Focused tests for the opaque keyset-cursor codec in
``app.services.collections_service``: a round-trip preserves the boundary
collection id, and a malformed cursor is rejected fail-closed as a
:class:`~app.core.errors.ValidationError` (INV-8 → 422) rather than silently
falling back to the first page. The end-to-end pagination behaviour is covered
against the real app in ``test_collections_api``.

Also covers the #269 object-orphan fix: deleting a collection removes the backing
content-addressed MinIO objects of its cascaded documents, guarded by
``count_by_storage_key`` (shared-content: a survivor in another collection keeps
the object). These run against an **offline** in-memory SQLite DB with a fake
object store, under ``autoflush=False`` (mirroring production ``db/session.py``),
since the post-delete ``flush`` is exactly what makes the guard count correctly.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.errors import ValidationError
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import DocumentStatus, Role
from app.services.audit import AuditSink
from app.services.collections_service import (
    CollectionsService,
    _clamp_limit,
    _decode_cursor,
    _encode_cursor,
)
from app.storage.keys import assert_key_owned_by
from tests._audit_helpers import (
    RecordingDurableAuditTransactions,
    denial_recorder_from_session,
)

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata


class _FakeObjectStore:
    """In-memory object store recording deletes (offline; no MinIO)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def delete(self, tenant_id: str, key: str) -> None:
        assert_key_owned_by(key, tenant_id)
        self.deleted.append(key)
        self.objects.pop(key, None)


def test_cursor_round_trips_collection_id() -> None:
    cid = uuid.uuid4()
    assert _decode_cursor(_encode_cursor(cid)) == cid


def test_cursor_is_opaque_base64() -> None:
    cid = uuid.uuid4()
    cursor = _encode_cursor(cid)
    # Opaque to the wire: not the bare uuid, and URL-safe.
    assert cursor != str(cid)
    assert cursor == cursor.strip()


@pytest.mark.parametrize(
    "bad",
    [
        "not-base64!!!",
        "",
        "Zm9vYmFy",  # base64 of "foobar" — no "col:" prefix
    ],
)
def test_malformed_cursor_raises_validation_error(bad: str) -> None:
    with pytest.raises(ValidationError):
        _decode_cursor(bad)


def test_cursor_with_prefix_but_bad_uuid_raises() -> None:
    encoded = base64.urlsafe_b64encode(b"col:not-a-uuid").decode()
    with pytest.raises(ValidationError):
        _decode_cursor(encoded)


def test_cursor_without_prefix_raises() -> None:
    # A valid uuid but minted without our prefix is rejected (not one of ours).
    encoded = base64.urlsafe_b64encode(str(uuid.uuid4()).encode()).decode()
    with pytest.raises(ValidationError):
        _decode_cursor(encoded)


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (None, 20),  # default
        (1, 1),
        (100, 100),
        (0, 1),  # below floor → clamped up
        (-5, 1),
        (250, 100),  # above ceiling → clamped down
    ],
)
def test_clamp_limit(requested: int | None, expected: int) -> None:
    assert _clamp_limit(requested) == expected


# --- Object cleanup on collection delete (#269) -----------------------------


@pytest_asyncio.fixture
async def sessionmaker(
    durable_audit_ledger: RecordingDurableAuditTransactions,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A StaticPool SQLite engine + schema, seeded with one tenant + owner.

    ``autoflush=False`` mirrors the production sessionmaker (``db/session.py``):
    the service's post-delete ``flush`` is exactly what makes
    ``count_by_storage_key`` see the pending row deletes (#269).
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(
            bind=engine,
            expire_on_commit=False,
            autoflush=False,
            info={"durable_audit_ledger": durable_audit_ledger},
        )
        async with factory() as seed:
            tenant = await TenantRepository(seed).create(name="Acme")
            owner = await UserRepository(seed, tenant.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            await seed.commit()
        factory.seeded = (tenant.id, owner.id)  # type: ignore[attr-defined]
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    return sessionmaker.seeded  # type: ignore[attr-defined, no-any-return]


def _key(tenant_id: uuid.UUID, sha: str, filename: str) -> str:
    """Build a valid ``{tenant}/{sha256}/{filename}`` content-addressed key."""
    return f"{tenant_id}/{sha}/{filename}"


def _service(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    store: _FakeObjectStore,
) -> CollectionsService:
    return CollectionsService(
        session,
        tenant_id=tenant_id,
        owner_id=owner_id,
        object_store=store,  # type: ignore[arg-type]
        audit=AuditSink(AuditEventRepository(session, tenant_id)),
        denials=denial_recorder_from_session(session, tenant_id),
        request_id="req-1",
        source_ip="203.0.113.5",
    )


async def test_delete_removes_distinct_backing_objects(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Two documents with DISTINCT content → both objects removed on delete (#269)."""
    tenant_id, owner_id = seeded
    store = _FakeObjectStore()
    async with sessionmaker() as session:
        collection = await CollectionRepository(session, tenant_id).create(
            owner_id=owner_id, name="C", description=None
        )
        documents = DocumentRepository(session, tenant_id)
        key_a = _key(tenant_id, "a" * 64, "a.txt")
        key_b = _key(tenant_id, "b" * 64, "b.txt")
        for key in (key_a, key_b):
            await documents.create(
                owner_id=owner_id,
                collection_id=collection.id,
                filename=key.rsplit("/", 1)[1],
                mime_type="text/plain",
                size_bytes=1,
                storage_key=key,
                acl_enforced=False,
                status=DocumentStatus.READY,
            )
        await session.commit()

        ok = await _service(session, tenant_id=tenant_id, owner_id=owner_id, store=store).delete(
            collection.id
        )
        await session.commit()

    assert ok is True
    assert set(store.deleted) == {key_a, key_b}


async def test_delete_removes_shared_object_exactly_once(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Two docs with the SAME storage_key → the shared object removed exactly once.

    Both share one content-addressed object; deleting the collection removes both
    rows, so no survivor references the key and the object is deleted a single
    time (the ``count_by_storage_key`` guard collapses the pair to one delete).
    """
    tenant_id, owner_id = seeded
    store = _FakeObjectStore()
    shared = _key(tenant_id, "c" * 64, "shared.txt")
    async with sessionmaker() as session:
        collection = await CollectionRepository(session, tenant_id).create(
            owner_id=owner_id, name="C", description=None
        )
        documents = DocumentRepository(session, tenant_id)
        for name in ("one.txt", "two.txt"):
            await documents.create(
                owner_id=owner_id,
                collection_id=collection.id,
                filename=name,
                mime_type="text/plain",
                size_bytes=1,
                storage_key=shared,
                acl_enforced=False,  # identical content-addressed key
                status=DocumentStatus.READY,
            )
        await session.commit()

        ok = await _service(session, tenant_id=tenant_id, owner_id=owner_id, store=store).delete(
            collection.id
        )
        await session.commit()

    assert ok is True
    assert store.deleted == [shared]  # exactly once, and gone


async def test_delete_keeps_object_referenced_by_survivor_in_other_collection(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A doc in ANOTHER collection sharing a key → the object is KEPT (#269 guard).

    Exercises ``count_by_storage_key``: the deleted collection's document shares a
    content-addressed key with a document in a *different* collection. Deleting the
    first collection must NOT remove the object — the survivor still references it.
    """
    tenant_id, owner_id = seeded
    store = _FakeObjectStore()
    shared = _key(tenant_id, "d" * 64, "shared.txt")
    async with sessionmaker() as session:
        collections = CollectionRepository(session, tenant_id)
        documents = DocumentRepository(session, tenant_id)
        coll_a = await collections.create(owner_id=owner_id, name="A", description=None)
        coll_b = await collections.create(owner_id=owner_id, name="B", description=None)
        await documents.create(
            owner_id=owner_id,
            collection_id=coll_a.id,
            filename="in-a.txt",
            mime_type="text/plain",
            size_bytes=1,
            storage_key=shared,
            acl_enforced=False,
            status=DocumentStatus.READY,
        )
        await documents.create(
            owner_id=owner_id,
            collection_id=coll_b.id,
            filename="in-b.txt",
            mime_type="text/plain",
            size_bytes=1,
            storage_key=shared,
            acl_enforced=False,  # survivor in the OTHER collection
            status=DocumentStatus.READY,
        )
        await session.commit()

        ok = await _service(session, tenant_id=tenant_id, owner_id=owner_id, store=store).delete(
            coll_a.id
        )
        await session.commit()

        # The object is kept: coll_b's document still references the shared key.
        assert ok is True
        assert store.deleted == []
        assert await DocumentRepository(session, tenant_id).count_by_storage_key(shared) == 1
