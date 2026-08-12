"""Direct upload v2 API: JSON control plane, idempotency and negatives (#571)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db_session, get_object_store_dep, get_settings_dep
from app.auth import hash_password
from app.core.config import get_settings
from app.core.errors import DependencyError, NotFoundError
from app.db import models
from app.db.base import Base
from app.db.repositories import (
    CollectionRepository,
    DocumentRepository,
    DocumentUploadRepository,
    TenantRepository,
    TranscriptRepository,
    TranscriptSegmentInput,
    TranscriptSpeakerInput,
    UserRepository,
)
from app.domain.audit import AuditAction
from app.domain.entities import (
    DocumentKind,
    DocumentStatus,
    DocumentUploadState,
    Role,
)
from app.main import create_app
from app.storage import MultipartUpload, StoredObjectMetadata, UploadedPart

import app.db.models  # noqa: F401  isort: skip

_PASSWORD = "devpassword"


class FakeMultipartStore:
    """Storage state machine fake; provider ids stay observable only to tests."""

    def __init__(self) -> None:
        self.uploads: dict[str, dict[str, object]] = {}
        self.objects: dict[str, StoredObjectMetadata] = {}
        self.aborted: list[str] = []
        self.deleted: list[str] = []
        self.complete_calls: list[str] = []
        self.presign_upload_calls: list[int] = []

    async def create_multipart_upload(
        self,
        *,
        tenant_id: str,
        document_id: str,
        upload_id: str,
        filename: str,
        content_type: str,
    ) -> MultipartUpload:
        key = f"{tenant_id}/quarantine/{document_id}/{filename}"
        provider = f"private-{upload_id}"
        self.uploads[provider] = {
            "key": key,
            "content_type": content_type,
            "metadata": {
                "lumen-upload-id": upload_id,
                "lumen-document-id": document_id,
            },
            "parts": [],
        }
        return MultipartUpload(key=key, provider_upload_id=provider)

    async def presign_upload_part(self, **kwargs: object) -> str:
        self.presign_upload_calls.append(int(str(kwargs["part_number"])))
        return f"https://storage.test/part?partNumber={kwargs['part_number']}" "&signature=fake"

    async def list_multipart_parts(self, **kwargs: object) -> list[UploadedPart]:
        return list(self.uploads[str(kwargs["provider_upload_id"])]["parts"])  # type: ignore[arg-type,call-overload]

    async def complete_multipart_upload(
        self, *, provider_upload_id: str, **kwargs: object
    ) -> StoredObjectMetadata:
        upload = self.uploads[provider_upload_id]
        self.complete_calls.append(provider_upload_id)
        parts = list(upload["parts"])  # type: ignore[arg-type]
        result = StoredObjectMetadata(
            key=str(upload["key"]),
            size_bytes=sum(part.size_bytes for part in parts),
            content_type=str(upload["content_type"]),
            metadata=dict(upload["metadata"]),  # type: ignore[arg-type]
        )
        self.objects[result.key] = result
        return result

    async def head(self, tenant_id: str, key: str) -> StoredObjectMetadata:
        try:
            return self.objects[key]
        except KeyError as exc:
            raise NotFoundError("Stored object not found.") from exc

    async def abort_multipart_upload(self, *, provider_upload_id: str, **kwargs: object) -> None:
        self.aborted.append(provider_upload_id)

    async def delete(self, tenant_id: str, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)

    async def presign_get(
        self,
        tenant_id: str,
        key: str,
        *,
        download_filename: str | None = None,
        content_type: str | None = None,
    ) -> str:
        return f"https://storage.test/{key}?signature=read"

    def upload_parts(self, provider_upload_id: str, sizes: list[int]) -> None:
        self.uploads[provider_upload_id]["parts"] = [
            UploadedPart(part_number=index, etag=f'"etag-{index}"', size_bytes=size)
            for index, size in enumerate(sizes, 1)
        ]


@dataclass(slots=True)
class Seeded:
    tenant_a: uuid.UUID
    tenant_b: uuid.UUID
    collection_a: uuid.UUID
    collection_b: uuid.UUID


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as seed:
        tenant_a = await TenantRepository(seed).create(name="Acme")
        tenant_b = await TenantRepository(seed).create(name="Globex")
        alice = await UserRepository(seed, tenant_a.id).create(
            email="alice@a.test", password_hash=hash_password(_PASSWORD), roles=[Role.MEMBER]
        )
        await UserRepository(seed, tenant_a.id).create(
            email="bob@a.test", password_hash=hash_password(_PASSWORD), roles=[Role.MEMBER]
        )
        carol = await UserRepository(seed, tenant_b.id).create(
            email="carol@b.test", password_hash=hash_password(_PASSWORD), roles=[Role.MEMBER]
        )
        collection_a = await CollectionRepository(seed, tenant_a.id).create(
            owner_id=alice.id, name="Alice"
        )
        collection_b = await CollectionRepository(seed, tenant_b.id).create(
            owner_id=carol.id, name="Carol"
        )
        await seed.commit()
    factory.seeded = Seeded(  # type: ignore[attr-defined]
        tenant_a.id, tenant_b.id, collection_a.id, collection_b.id
    )
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def store() -> FakeMultipartStore:
    return FakeMultipartStore()


@pytest.fixture
def app(
    sessionmaker: async_sessionmaker[AsyncSession], store: FakeMultipartStore
) -> Iterator[FastAPI]:
    settings = get_settings().model_copy(update={"upload_part_size_bytes": 5 * 1024 * 1024})
    application = create_app(settings)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_db_session] = override_session
    application.dependency_overrides[get_object_store_dep] = lambda: store
    application.dependency_overrides[get_settings_dep] = lambda: settings
    yield application
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def login(client: AsyncClient, email: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return str(response.json()["access_token"])


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def initiate(
    client: AsyncClient, token: str, collection_id: uuid.UUID, *, size: int = 6 * 1024 * 1024
):
    return await client.post(
        "/api/v2/document-uploads",
        headers=auth(token),
        json={
            "filename": "meeting.mp3",
            "mime_type": "audio/mpeg",
            "size_bytes": size,
            "collection_id": str(collection_id),
        },
    )


async def upload_rejection_audits(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> list[models.AuditEvent]:
    """Read durable upload rejection evidence from a fresh transaction."""
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(models.AuditEvent).where(models.AuditEvent.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
    return [row for row in rows if "operation" in row.event_metadata]


def assert_content_safe_rejection(event: models.AuditEvent) -> None:
    """Negative audit evidence contains policy facts, never upload/provider data."""
    assert set(event.event_metadata) == {"operation", "reason_code", "status"}
    serialized = repr(event.event_metadata).lower()
    for forbidden in (
        "private-",
        "storage_key",
        "provider_upload_id",
        "https://",
        "transcript",
        "payload.exe",
        "application/pdf",
    ):
        assert forbidden not in serialized


async def test_legacy_binary_path_is_authenticated_410_without_file_parsing(
    client: AsyncClient,
) -> None:
    assert (await client.post("/api/v1/documents", content=b"secret bytes")).status_code == 401
    token = await login(client, "alice@a.test")
    response = await client.post(
        "/api/v1/documents",
        headers={**auth(token), "content-type": "application/octet-stream"},
        content=b"secret bytes",
    )
    assert response.status_code == 410
    assert response.json()["code"] == "document_upload_retired"


async def test_initiate_sign_complete_is_idempotent_and_hides_provider_id(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeMultipartStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    token = await login(client, "alice@a.test")
    enqueued: list[tuple[uuid.UUID, uuid.UUID, bool]] = []

    def record_enqueue(tenant_id: uuid.UUID, document_id: uuid.UUID, *, media: bool) -> None:
        enqueued.append((tenant_id, document_id, media))

    monkeypatch.setattr("app.tasks.enqueue_ingestion", record_enqueue)
    started = await initiate(client, token, seeded.collection_a)
    assert started.status_code == 201, started.text
    body = started.json()
    serialized = started.text
    provider_id = next(iter(store.uploads))
    assert provider_id not in serialized
    assert "storage_key" not in serialized

    signed = await client.post(
        f"/api/v2/document-uploads/{body['id']}/parts",
        headers=auth(token),
        json={"part_numbers": [1, 2]},
    )
    assert signed.status_code == 200, signed.text
    assert provider_id not in signed.text
    assert all("Authorization" not in item["required_headers"] for item in signed.json()["items"])

    store.upload_parts(provider_id, [5 * 1024 * 1024, 1 * 1024 * 1024])
    completion = {
        "parts": [
            {"part_number": 1, "etag": '"etag-1"'},
            {"part_number": 2, "etag": '"etag-2"'},
        ]
    }
    first = await client.post(
        f"/api/v2/document-uploads/{body['id']}/complete",
        headers=auth(token),
        json=completion,
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        f"/api/v2/document-uploads/{body['id']}/complete",
        headers=auth(token),
        json=completion,
    )
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert enqueued == [(seeded.tenant_a, uuid.UUID(first.json()["id"]), True)]

    deleted = await client.delete(f"/api/v1/documents/{first.json()['id']}", headers=auth(token))
    assert deleted.status_code == 204
    retired_session = await client.get(
        f"/api/v2/document-uploads/{body['id']}", headers=auth(token)
    )
    assert retired_session.status_code == 404


async def test_initiation_rejects_foreign_collection_type_and_oversize(
    client: AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    token = await login(client, "alice@a.test")
    foreign = await initiate(client, token, seeded.collection_b)
    assert foreign.status_code == 404
    unsupported = await client.post(
        "/api/v2/document-uploads",
        headers=auth(token),
        json={
            "filename": "bad.exe",
            "mime_type": "application/x-msdownload",
            "size_bytes": 10,
            "collection_id": str(seeded.collection_a),
        },
    )
    assert unsupported.status_code == 415
    too_large = await initiate(client, token, seeded.collection_a, size=6 * 1024**3)
    assert too_large.status_code == 413


@pytest.mark.parametrize(
    ("filename", "declared", "canonical"),
    [
        ("meeting.MP3", "audio/mp3", "audio/mpeg"),
        ("voice.wav", "audio/vnd.wave", "audio/wav"),
        ("voice.wav", "audio/x-wav", "audio/wav"),
        ("recording.m4a", "audio/x-m4a", "audio/mp4"),
        ("recording.flac", "application/octet-stream", "audio/flac"),
        ("audio-only.mp4", "audio/mp4", "audio/mp4"),
        ("camera.mp4", "video/mp4; codecs=avc1", "video/mp4"),
        ("audio-only.webm", "audio/webm", "audio/webm"),
        ("camera.webm", "video/webm", "video/webm"),
    ],
)
async def test_initiation_canonicalizes_only_safe_filename_mime_aliases(
    filename: str,
    declared: str,
    canonical: str,
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeMultipartStore,
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    token = await login(client, "alice@a.test")
    response = await client.post(
        "/api/v2/document-uploads",
        headers=auth(token),
        json={
            "filename": filename,
            "mime_type": declared,
            "size_bytes": 10,
            "collection_id": str(seeded.collection_a),
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["mime_type"] == canonical
    provider = next(iter(store.uploads.values()))
    assert provider["content_type"] == canonical


async def test_initiation_rejections_are_audited_and_never_reach_storage(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeMultipartStore,
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    token = await login(client, "alice@a.test")
    cases = [
        ("payload.exe", "application/pdf", 10, 415, "filename_extension_not_allowed"),
        ("payload.pdf", "text/plain", 10, 422, "filename_content_type_mismatch"),
        ("   ", "application/pdf", 10, 422, "missing_filename"),
        ("payload.pdf", "application/pdf", 0, 422, "empty_upload"),
        (
            "meeting.mp3",
            "audio/mpeg",
            6 * 1024**3,
            413,
            "upload_too_large",
        ),
    ]

    for filename, mime_type, size_bytes, status_code, code in cases:
        response = await client.post(
            "/api/v2/document-uploads",
            headers=auth(token),
            json={
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
                "collection_id": str(seeded.collection_a),
            },
        )
        assert response.status_code == status_code, response.text
        assert response.json()["code"] == code

    assert store.uploads == {}
    assert store.presign_upload_calls == []
    assert store.aborted == []
    assert store.deleted == []
    assert store.complete_calls == []
    audits = await upload_rejection_audits(sessionmaker, seeded.tenant_a)
    assert {event.event_metadata["reason_code"] for event in audits} == {case[4] for case in cases}
    for event in audits:
        assert event.action == AuditAction.DOCUMENT_UPLOAD_STARTED.value
        assert event.outcome == "error"
        assert event.resource_type == "collection"
        assert_content_safe_rejection(event)


async def test_missing_foreign_and_nonowned_upload_controls_are_hidden_audited_and_io_free(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeMultipartStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    alice = await login(client, "alice@a.test")
    bob = await login(client, "bob@a.test")
    carol = await login(client, "carol@b.test")

    for collection_id in (seeded.collection_b, uuid.uuid4()):
        response = await initiate(client, alice, collection_id)
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"
    assert store.uploads == {}

    started = await initiate(client, alice, seeded.collection_a)
    assert started.status_code == 201
    upload_id = started.json()["id"]
    missing_id = str(uuid.uuid4())

    async def unexpected_storage_call(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a scoped 404 reached object storage")

    for name in (
        "presign_upload_part",
        "list_multipart_parts",
        "complete_multipart_upload",
        "head",
        "abort_multipart_upload",
        "delete",
    ):
        monkeypatch.setattr(store, name, unexpected_storage_call)

    async def attempt(operation: str, token: str, requested_id: str) -> Response:
        url = f"/api/v2/document-uploads/{requested_id}"
        if operation == "get":
            return await client.get(url, headers=auth(token))
        if operation == "sign_parts":
            return await client.post(
                f"{url}/parts", headers=auth(token), json={"part_numbers": [1]}
            )
        if operation == "complete":
            return await client.post(
                f"{url}/complete",
                headers=auth(token),
                json={"parts": [{"part_number": 1, "etag": '"etag-1"'}]},
            )
        return await client.delete(url, headers=auth(token))

    operations = ("get", "sign_parts", "complete", "abort")
    for operation in operations:
        for token, requested_id in (
            (bob, upload_id),
            (carol, upload_id),
            (alice, missing_id),
        ):
            response = await attempt(operation, token, requested_id)
            assert response.status_code == 404, response.text
            assert response.json()["code"] == "not_found"

    tenant_a_audits = await upload_rejection_audits(sessionmaker, seeded.tenant_a)
    tenant_b_audits = await upload_rejection_audits(sessionmaker, seeded.tenant_b)
    assert len(tenant_a_audits) == 2 + 2 * len(operations)
    assert len(tenant_b_audits) == len(operations)
    for event in [*tenant_a_audits, *tenant_b_audits]:
        assert event.action == AuditAction.PERMISSION_DENIED.value
        assert event.outcome == "denied"
        assert event.event_metadata["reason_code"] == "not_found_or_not_owned"
        assert_content_safe_rejection(event)


async def test_invalid_part_requests_and_manifests_are_durably_audited_without_mutation(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeMultipartStore,
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    token = await login(client, "alice@a.test")
    started = await initiate(client, token, seeded.collection_a)
    upload_id = started.json()["id"]
    provider_id = next(iter(store.uploads))

    invalid_signs = [
        ({"part_numbers": []}, "invalid_part_batch"),
        ({"part_numbers": [1, 1]}, "invalid_part_batch"),
        ({"part_numbers": [3]}, "part_number_out_of_range"),
    ]
    for payload, code in invalid_signs:
        response = await client.post(
            f"/api/v2/document-uploads/{upload_id}/parts",
            headers=auth(token),
            json=payload,
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == code
    assert store.presign_upload_calls == []

    store.upload_parts(provider_id, [5 * 1024 * 1024, 1 * 1024 * 1024])
    invalid_completions = [
        (
            {"parts": [{"part_number": 1, "etag": '"etag-1"'}]},
            "invalid_part_manifest",
        ),
        (
            {
                "parts": [
                    {"part_number": 1, "etag": "bad\netag"},
                    {"part_number": 2, "etag": '"etag-2"'},
                ]
            },
            "invalid_part_etag",
        ),
        (
            {
                "parts": [
                    {"part_number": 1, "etag": '"different"'},
                    {"part_number": 2, "etag": '"etag-2"'},
                ]
            },
            "part_etag_mismatch",
        ),
    ]
    for payload, code in invalid_completions:
        response = await client.post(
            f"/api/v2/document-uploads/{upload_id}/complete",
            headers=auth(token),
            json=payload,
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == code

    store.upload_parts(provider_id, [5 * 1024 * 1024, 2])
    bad_layout = await client.post(
        f"/api/v2/document-uploads/{upload_id}/complete",
        headers=auth(token),
        json={
            "parts": [
                {"part_number": 1, "etag": '"etag-1"'},
                {"part_number": 2, "etag": '"etag-2"'},
            ]
        },
    )
    assert bad_layout.status_code == 422
    assert bad_layout.json()["code"] == "invalid_provider_part_layout"
    assert store.complete_calls == []
    assert store.aborted == []
    assert store.deleted == []

    audits = await upload_rejection_audits(sessionmaker, seeded.tenant_a)
    expected = {
        "invalid_part_batch",
        "part_number_out_of_range",
        "invalid_part_manifest",
        "invalid_part_etag",
        "part_etag_mismatch",
        "invalid_provider_part_layout",
    }
    assert {event.event_metadata["reason_code"] for event in audits} == expected
    for event in audits:
        assert event.outcome == "error"
        assert event.resource_id == upload_id
        assert_content_safe_rejection(event)


async def test_initiation_locks_collection_against_concurrent_delete(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    token = await login(client, "alice@a.test")
    original = CollectionRepository.get
    lock_values: list[bool] = []

    async def recording_get(
        repo: CollectionRepository, collection_id: uuid.UUID, *, lock: bool = False
    ):
        lock_values.append(lock)
        return await original(repo, collection_id, lock=lock)

    monkeypatch.setattr(CollectionRepository, "get", recording_get)
    response = await initiate(client, token, seeded.collection_a)
    assert response.status_code == 201
    assert lock_values == [True]


async def test_storage_outages_return_opaque_503_across_upload_control_plane(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeMultipartStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    token = await login(client, "alice@a.test")

    async def unavailable(*_args: object, **_kwargs: object) -> None:
        raise DependencyError("Object storage is unavailable.", code="storage_unavailable")

    original_create = store.create_multipart_upload
    monkeypatch.setattr(store, "create_multipart_upload", unavailable)
    initiation = await initiate(client, token, seeded.collection_a)
    assert initiation.status_code == 503
    assert initiation.json()["code"] == "storage_unavailable"
    assert "private" not in initiation.text
    monkeypatch.setattr(store, "create_multipart_upload", original_create)

    started_for_sign = await initiate(client, token, seeded.collection_a)
    monkeypatch.setattr(store, "presign_upload_part", unavailable)
    signed = await client.post(
        f"/api/v2/document-uploads/{started_for_sign.json()['id']}/parts",
        headers=auth(token),
        json={"part_numbers": [1]},
    )
    assert signed.status_code == 503
    assert signed.json()["code"] == "storage_unavailable"

    started_for_abort = await initiate(client, token, seeded.collection_a)
    monkeypatch.setattr(store, "abort_multipart_upload", unavailable)
    aborted = await client.delete(
        f"/api/v2/document-uploads/{started_for_abort.json()['id']}",
        headers=auth(token),
    )
    assert aborted.status_code == 503
    assert aborted.json()["code"] == "storage_unavailable"

    async with sessionmaker() as session:
        owner = await UserRepository(session, seeded.tenant_a).get_by_email("alice@a.test")
        assert owner is not None
        document = await DocumentRepository(session, seeded.tenant_a).create(
            owner_id=owner.id,
            collection_id=seeded.collection_a,
            filename="meeting.mp3",
            mime_type="audio/mpeg",
            size_bytes=8,
            storage_key=f"{seeded.tenant_a}/meeting.mp3",
            acl_enforced=False,
            status=DocumentStatus.READY,
            kind=DocumentKind.AUDIO,
        )
        await session.commit()
    monkeypatch.setattr(store, "presign_get", unavailable)
    access = await client.post(
        f"/api/v2/documents/{document.id}/access-url",
        headers=auth(token),
        json={"purpose": "preview"},
    )
    assert access.status_code == 503
    assert access.json()["code"] == "storage_unavailable"


async def test_foreign_media_access_and_transcript_are_hidden_and_durably_audited(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeMultipartStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-1/2/6: capability and transcript denials are 404 with safe evidence."""
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    alice = await login(client, "alice@a.test")
    async with sessionmaker() as session:
        owner = await UserRepository(session, seeded.tenant_b).get_by_email("carol@b.test")
        assert owner is not None
        foreign = await DocumentRepository(session, seeded.tenant_b).create(
            owner_id=owner.id,
            collection_id=seeded.collection_b,
            filename="private-meeting.mp3",
            mime_type="audio/mpeg",
            size_bytes=8,
            storage_key=f"{seeded.tenant_b}/private-meeting.mp3",
            acl_enforced=False,
            status=DocumentStatus.READY,
            kind=DocumentKind.AUDIO,
        )
        await session.commit()

    async def unexpected_presign(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("a hidden document reached object storage")

    monkeypatch.setattr(store, "presign_get", unexpected_presign)
    access = await client.post(
        f"/api/v2/documents/{foreign.id}/access-url",
        headers=auth(alice),
        json={"purpose": "preview"},
    )
    transcript = await client.get(
        f"/api/v2/documents/{foreign.id}/transcript",
        headers=auth(alice),
    )
    assert access.status_code == transcript.status_code == 404
    assert access.json()["code"] == transcript.json()["code"] == "not_found"

    async with sessionmaker() as session:
        denials = (
            (
                await session.execute(
                    select(models.AuditEvent).where(
                        models.AuditEvent.tenant_id == seeded.tenant_a,
                        models.AuditEvent.resource_id == str(foreign.id),
                        models.AuditEvent.action == AuditAction.PERMISSION_DENIED.value,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(denials) == 2
    assert {event.event_metadata["operation"] for event in denials} == {
        "access_url",
        "transcript",
    }
    for event in denials:
        assert event.outcome == "denied"
        assert event.event_metadata == {
            "operation": event.event_metadata["operation"],
            "reason_code": "not_found_or_not_permitted",
            "status": 404,
        }


async def test_upload_session_is_owner_tenant_hidden_and_abort_idempotent(
    client: AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    alice = await login(client, "alice@a.test")
    carol = await login(client, "carol@b.test")
    started = await initiate(client, alice, seeded.collection_a)
    upload_id = started.json()["id"]
    assert (
        await client.get(f"/api/v2/document-uploads/{upload_id}", headers=auth(carol))
    ).status_code == 404
    for _ in range(2):
        response = await client.delete(f"/api/v2/document-uploads/{upload_id}", headers=auth(alice))
        assert response.status_code == 204


async def test_completion_size_mismatch_fails_session_and_never_enqueues(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeMultipartStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    token = await login(client, "alice@a.test")
    enqueued: list[object] = []
    monkeypatch.setattr("app.tasks.enqueue_ingestion", lambda *args: enqueued.append(args))
    started = await initiate(client, token, seeded.collection_a)
    provider_id = next(iter(store.uploads))
    # Provider part list is internally consistent with declared sizes, but the
    # final HEAD lies: simulate post-complete truncation by changing metadata size.
    store.upload_parts(provider_id, [5 * 1024 * 1024, 1 * 1024 * 1024])
    original = store.complete_multipart_upload

    async def mismatched(**kwargs: object) -> StoredObjectMetadata:
        result = await original(**kwargs)
        return StoredObjectMetadata(
            result.key, result.size_bytes - 1, result.content_type, result.metadata
        )

    monkeypatch.setattr(store, "complete_multipart_upload", mismatched)
    response = await client.post(
        f"/api/v2/document-uploads/{started.json()['id']}/complete",
        headers=auth(token),
        json={
            "parts": [
                {"part_number": 1, "etag": '"etag-1"'},
                {"part_number": 2, "etag": '"etag-2"'},
            ]
        },
    )
    assert response.status_code == 413, response.text
    assert enqueued == []
    state = await client.get(
        f"/api/v2/document-uploads/{started.json()['id']}", headers=auth(token)
    )
    assert state.json()["state"] == "failed"
    audits = await upload_rejection_audits(sessionmaker, seeded.tenant_a)
    assert len(audits) == 1
    rejection = audits[0]
    assert rejection.action == AuditAction.DOCUMENT_UPLOADED.value
    assert rejection.outcome == "error"
    assert rejection.event_metadata["reason_code"] == "stored_size_mismatch"
    assert_content_safe_rejection(rejection)


async def test_completion_metadata_mismatch_fails_closed_and_commits_safe_audit(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeMultipartStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    token = await login(client, "alice@a.test")
    enqueued: list[object] = []
    monkeypatch.setattr("app.tasks.enqueue_ingestion", lambda *args: enqueued.append(args))
    started = await initiate(client, token, seeded.collection_a)
    upload_id = started.json()["id"]
    provider_id = next(iter(store.uploads))
    store.upload_parts(provider_id, [5 * 1024 * 1024, 1 * 1024 * 1024])
    original = store.complete_multipart_upload

    async def mismatched(**kwargs: object) -> StoredObjectMetadata:
        result = await original(**kwargs)
        return StoredObjectMetadata(
            result.key,
            result.size_bytes,
            result.content_type,
            {**result.metadata, "lumen-upload-id": str(uuid.uuid4())},
        )

    monkeypatch.setattr(store, "complete_multipart_upload", mismatched)
    response = await client.post(
        f"/api/v2/document-uploads/{upload_id}/complete",
        headers=auth(token),
        json={
            "parts": [
                {"part_number": 1, "etag": '"etag-1"'},
                {"part_number": 2, "etag": '"etag-2"'},
            ]
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "stored_metadata_mismatch"
    assert enqueued == []
    assert store.objects == {}
    assert store.deleted

    state = await client.get(f"/api/v2/document-uploads/{upload_id}", headers=auth(token))
    assert state.status_code == 200
    assert state.json()["state"] == "failed"
    async with sessionmaker() as session:
        document_count = (
            await session.execute(
                select(models.Document).where(
                    models.Document.id == uuid.UUID(started.json()["document_id"])
                )
            )
        ).scalar_one_or_none()
    assert document_count is None
    audits = await upload_rejection_audits(sessionmaker, seeded.tenant_a)
    assert len(audits) == 1
    rejection = audits[0]
    assert rejection.action == AuditAction.DOCUMENT_UPLOADED.value
    assert rejection.outcome == "error"
    assert rejection.event_metadata["reason_code"] == "stored_metadata_mismatch"
    assert_content_safe_rejection(rejection)


async def test_illegal_upload_state_attempts_are_audited_without_further_storage_io(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeMultipartStore,
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    token = await login(client, "alice@a.test")
    started = await initiate(client, token, seeded.collection_a)
    upload_id = started.json()["id"]
    assert (
        await client.delete(f"/api/v2/document-uploads/{upload_id}", headers=auth(token))
    ).status_code == 204
    mutations = (list(store.aborted), list(store.deleted), list(store.complete_calls))

    signed = await client.post(
        f"/api/v2/document-uploads/{upload_id}/parts",
        headers=auth(token),
        json={"part_numbers": [1]},
    )
    assert signed.status_code == 409
    assert signed.json()["code"] == "upload_state_conflict"
    completed = await client.post(
        f"/api/v2/document-uploads/{upload_id}/complete",
        headers=auth(token),
        json={
            "parts": [
                {"part_number": 1, "etag": '"etag-1"'},
                {"part_number": 2, "etag": '"etag-2"'},
            ]
        },
    )
    assert completed.status_code == 409
    assert completed.json()["code"] == "upload_state_conflict"
    assert (store.aborted, store.deleted, store.complete_calls) == mutations

    completed_session = await initiate(client, token, seeded.collection_a)
    completed_upload_id = uuid.UUID(completed_session.json()["id"])
    async with sessionmaker() as session:
        owner = await UserRepository(session, seeded.tenant_a).get_by_email("alice@a.test")
        assert owner is not None
        await DocumentUploadRepository(session, seeded.tenant_a).set_state(
            completed_upload_id, owner.id, DocumentUploadState.COMPLETED
        )
        await session.commit()
    mutation_count = (len(store.aborted), len(store.deleted), len(store.complete_calls))
    aborted = await client.delete(
        f"/api/v2/document-uploads/{completed_upload_id}", headers=auth(token)
    )
    assert aborted.status_code == 409
    assert aborted.json()["code"] == "upload_state_conflict"
    assert (len(store.aborted), len(store.deleted), len(store.complete_calls)) == mutation_count

    audits = await upload_rejection_audits(sessionmaker, seeded.tenant_a)
    by_operation = {event.event_metadata["operation"]: event for event in audits}
    assert set(by_operation) == {"sign_parts", "complete", "abort"}
    for event in by_operation.values():
        assert event.event_metadata["reason_code"] == "upload_state_conflict"
        assert event.outcome == "error"
        assert_content_safe_rejection(event)


async def test_completion_recovers_after_provider_success_process_crash(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeMultipartStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    token = await login(client, "alice@a.test")
    enqueued: list[tuple[uuid.UUID, uuid.UUID, bool]] = []

    def record_enqueue(tenant_id: uuid.UUID, document_id: uuid.UUID, *, media: bool) -> None:
        enqueued.append((tenant_id, document_id, media))

    monkeypatch.setattr("app.tasks.enqueue_ingestion", record_enqueue)
    started = await initiate(client, token, seeded.collection_a)
    upload_id = started.json()["id"]
    provider_id = next(iter(store.uploads))
    store.upload_parts(provider_id, [5 * 1024 * 1024, 1 * 1024 * 1024])
    original = store.complete_multipart_upload

    async def crash_after_provider_success(**kwargs: object) -> StoredObjectMetadata:
        await original(**kwargs)
        raise DependencyError("Simulated storage response loss.", code="storage_unavailable")

    monkeypatch.setattr(store, "complete_multipart_upload", crash_after_provider_success)
    interrupted = await client.post(
        f"/api/v2/document-uploads/{upload_id}/complete",
        headers=auth(token),
        json={
            "parts": [
                {"part_number": 1, "etag": '"etag-1"'},
                {"part_number": 2, "etag": '"etag-2"'},
            ]
        },
    )
    assert interrupted.status_code == 503
    assert interrupted.json()["code"] == "storage_unavailable"

    monkeypatch.setattr(store, "complete_multipart_upload", original)
    recovered = await client.get(f"/api/v2/document-uploads/{upload_id}", headers=auth(token))
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["state"] == "completed"
    document_id = uuid.UUID(recovered.json()["document"]["id"])
    assert enqueued == [(seeded.tenant_a, document_id, True)]

    # A second recovery read is side-effect free: no duplicate document/audit/task.
    repeated = await client.get(f"/api/v2/document-uploads/{upload_id}", headers=auth(token))
    assert repeated.status_code == 200
    assert enqueued == [(seeded.tenant_a, document_id, True)]


async def test_get_recovers_crash_after_durable_boundary_before_provider_completion(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeMultipartStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    token = await login(client, "alice@a.test")
    enqueued: list[tuple[uuid.UUID, uuid.UUID, bool]] = []
    monkeypatch.setattr(
        "app.tasks.enqueue_ingestion",
        lambda tenant_id, document_id, *, media: enqueued.append((tenant_id, document_id, media)),
    )
    started = await initiate(client, token, seeded.collection_a)
    upload_id = started.json()["id"]
    provider_id = next(iter(store.uploads))
    store.upload_parts(provider_id, [5 * 1024 * 1024, 1 * 1024 * 1024])
    original = store.complete_multipart_upload

    async def crash_before_provider_completion(**kwargs: object) -> StoredObjectMetadata:
        raise DependencyError("Simulated provider outage.", code="storage_unavailable")

    monkeypatch.setattr(store, "complete_multipart_upload", crash_before_provider_completion)
    interrupted = await client.post(
        f"/api/v2/document-uploads/{upload_id}/complete",
        headers=auth(token),
        json={
            "parts": [
                {"part_number": 1, "etag": '"etag-1"'},
                {"part_number": 2, "etag": '"etag-2"'},
            ]
        },
    )
    assert interrupted.status_code == 503
    assert interrupted.json()["code"] == "storage_unavailable"
    async with sessionmaker() as session:
        state = (
            await session.execute(
                select(models.DocumentUpload.state).where(
                    models.DocumentUpload.id == uuid.UUID(upload_id)
                )
            )
        ).scalar_one()
        assert state == DocumentUploadState.COMPLETING.value
    assert store.complete_calls == []

    # A fresh browser has only the upload id. GET reconstructs the manifest
    # from authoritative provider parts, completes, and returns the document.
    monkeypatch.setattr(store, "complete_multipart_upload", original)
    recovered = await client.get(f"/api/v2/document-uploads/{upload_id}", headers=auth(token))
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["state"] == "completed"
    document_id = uuid.UUID(recovered.json()["document"]["id"])
    assert store.complete_calls == [provider_id]
    assert enqueued == [(seeded.tenant_a, document_id, True)]


async def test_expiry_transition_and_audit_commit_before_parts_conflict(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeMultipartStore,
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    token = await login(client, "alice@a.test")
    started = await initiate(client, token, seeded.collection_a)
    upload_id = uuid.UUID(started.json()["id"])
    provider_id = next(iter(store.uploads))
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(models.DocumentUpload).where(models.DocumentUpload.id == upload_id)
            )
        ).scalar_one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    response = await client.post(
        f"/api/v2/document-uploads/{upload_id}/parts",
        headers=auth(token),
        json={"part_numbers": [1]},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "upload_session_expired"

    async with sessionmaker() as session:
        state = (
            await session.execute(
                select(models.DocumentUpload.state).where(models.DocumentUpload.id == upload_id)
            )
        ).scalar_one()
        audits = (
            (
                await session.execute(
                    select(models.AuditEvent).where(
                        models.AuditEvent.action == AuditAction.DOCUMENT_UPLOAD_EXPIRED.value,
                        models.AuditEvent.resource_id == str(upload_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert state == DocumentUploadState.EXPIRED.value
    assert len(audits) == 1
    assert provider_id in store.aborted


async def test_transcript_around_time_is_half_open_and_bad_cursor_is_422(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    token = await login(client, "alice@a.test")
    async with sessionmaker() as session:
        owner = await UserRepository(session, seeded.tenant_a).get_by_email("alice@a.test")
        assert owner is not None
        document = await DocumentRepository(session, seeded.tenant_a).create(
            owner_id=owner.id,
            collection_id=seeded.collection_a,
            filename="short.mp3",
            mime_type="audio/mpeg",
            size_bytes=8,
            storage_key=f"{seeded.tenant_a}/short.mp3",
            acl_enforced=False,
            status=DocumentStatus.READY,
            kind=DocumentKind.AUDIO,
        )
        document = await DocumentRepository(session, seeded.tenant_a).update_media_metadata(
            document.id,
            kind=DocumentKind.AUDIO,
            duration_ms=1_000,
            transcript_language="en",
            transcription_model="x-ai/grok-stt-1.0",
        )
        assert document is not None
        await TranscriptRepository(session, seeded.tenant_a).replace_for_document(
            document.id,
            speakers=[TranscriptSpeakerInput(speaker_id="speaker-1")],
            segments=[
                TranscriptSegmentInput(
                    id=uuid.uuid4(),
                    ordinal=0,
                    speaker_id="speaker-1",
                    start_ms=0,
                    end_ms=1_000,
                    char_start=0,
                    char_end=5,
                    text="hello",
                )
            ],
        )
        await session.commit()

    url = f"/api/v2/documents/{document.id}/transcript"
    at_start = await client.get(url, headers=auth(token), params={"around_ms": 0})
    assert at_start.status_code == 200, at_start.text
    assert at_start.json()["items"][0]["start_ms"] == 0
    before_end = await client.get(url, headers=auth(token), params={"around_ms": 999})
    assert before_end.status_code == 200
    at_end = await client.get(url, headers=auth(token), params={"around_ms": 1_000})
    assert at_end.status_code == 422
    assert at_end.json()["code"] == "around_ms_out_of_range"
    malformed = await client.get(url, headers=auth(token), params={"cursor": "A"})
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "invalid_cursor"


@pytest.mark.parametrize("operation", ["parts", "abort"])
async def test_illegal_sign_or_abort_commits_completing_object_recovery(
    operation: str,
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeMultipartStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    token = await login(client, "alice@a.test")
    enqueued: list[tuple[uuid.UUID, uuid.UUID, bool]] = []
    monkeypatch.setattr(
        "app.tasks.enqueue_ingestion",
        lambda tenant_id, document_id, *, media: enqueued.append((tenant_id, document_id, media)),
    )
    started = await initiate(client, token, seeded.collection_a)
    body = started.json()
    upload_id = uuid.UUID(body["id"])
    document_id = uuid.UUID(body["document_id"])
    provider_id = next(iter(store.uploads))
    provider = store.uploads[provider_id]
    key = str(provider["key"])
    store.objects[key] = StoredObjectMetadata(
        key=key,
        size_bytes=body["size_bytes"],
        content_type="audio/mpeg",
        metadata={
            "lumen-upload-id": str(upload_id),
            "lumen-document-id": str(document_id),
        },
    )
    async with sessionmaker() as session:
        owner = await UserRepository(session, seeded.tenant_a).get_by_email("alice@a.test")
        assert owner is not None
        await DocumentUploadRepository(session, seeded.tenant_a).set_state(
            upload_id, owner.id, DocumentUploadState.COMPLETING
        )
        await session.commit()

    if operation == "parts":
        response = await client.post(
            f"/api/v2/document-uploads/{upload_id}/parts",
            headers=auth(token),
            json={"part_numbers": [1]},
        )
    else:
        response = await client.delete(f"/api/v2/document-uploads/{upload_id}", headers=auth(token))
    assert response.status_code == 409

    recovered = await client.get(f"/api/v2/document-uploads/{upload_id}", headers=auth(token))
    assert recovered.status_code == 200
    assert recovered.json()["state"] == "completed"
    assert recovered.json()["document"]["id"] == str(document_id)
    assert enqueued == [(seeded.tenant_a, document_id, True)]


async def test_two_completers_cross_durable_boundary_only_complete_provider_once(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeMultipartStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded: Seeded = sessionmaker.seeded  # type: ignore[attr-defined]
    token = await login(client, "alice@a.test")
    enqueued: list[tuple[uuid.UUID, uuid.UUID, bool]] = []
    monkeypatch.setattr(
        "app.tasks.enqueue_ingestion",
        lambda tenant_id, document_id, *, media: enqueued.append((tenant_id, document_id, media)),
    )
    started = await initiate(client, token, seeded.collection_a)
    upload_id = started.json()["id"]
    provider_id = next(iter(store.uploads))
    store.upload_parts(provider_id, [5 * 1024 * 1024, 1 * 1024 * 1024])
    completion = {
        "parts": [
            {"part_number": 1, "etag": '"etag-1"'},
            {"part_number": 2, "etag": '"etag-2"'},
        ]
    }

    original_get = DocumentUploadRepository.get_for_owner
    get_calls = 0
    first_at_reacquire = asyncio.Event()
    second_finished = asyncio.Event()

    async def gated_get(
        repo: DocumentUploadRepository,
        requested_upload_id: uuid.UUID,
        owner_id: uuid.UUID,
        *,
        lock: bool = False,
    ):
        nonlocal get_calls
        get_calls += 1
        # Only the first request is running until this third lookup (route
        # expiry check, complete entry, then post-boundary reacquisition).
        if get_calls == 3:
            first_at_reacquire.set()
            await second_finished.wait()
        return await original_get(repo, requested_upload_id, owner_id, lock=lock)

    monkeypatch.setattr(DocumentUploadRepository, "get_for_owner", gated_get)

    async def first() -> Response:
        return await client.post(
            f"/api/v2/document-uploads/{upload_id}/complete",
            headers=auth(token),
            json=completion,
        )

    async def second() -> Response:
        await first_at_reacquire.wait()
        try:
            return await client.post(
                f"/api/v2/document-uploads/{upload_id}/complete",
                headers=auth(token),
                json=completion,
            )
        finally:
            second_finished.set()

    first_task = asyncio.create_task(first(), name="complete-a")
    second_task = asyncio.create_task(second(), name="complete-b")
    first_response, second_response = await asyncio.wait_for(
        asyncio.gather(first_task, second_task), timeout=10
    )
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["id"] == second_response.json()["id"]
    assert store.complete_calls == [provider_id]
    document_id = uuid.UUID(first_response.json()["id"])
    assert enqueued == [(seeded.tenant_a, document_id, True)]
