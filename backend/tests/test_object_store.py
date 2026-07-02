"""Tests for the object-storage adapter (issue #22, CC-12).

Three layers:

* **Pure unit** (zero mocks) — key construction, filename sanitization, the
  tenant-prefix seam, and upload validation. These pin the AC-2 / AC-4 / AC-5 /
  AC-6 invariants without touching S3.
* **Adapter unit** (mocked S3 client) — ``put``/``get``/``delete``/``presign_*``
  call the client correctly, and the tenant-prefix refusal short-circuits before
  any I/O.
* **Integration** (live compose MinIO) — a real ``put`` → ``get`` round-trip
  with teardown (AC-1). Skipped automatically if MinIO is unreachable, so the
  suite stays green offline.
"""

from __future__ import annotations

import contextlib
import os
import socket
import uuid
from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlparse

import pytest

from app.core.config import Settings
from app.core.errors import ForbiddenError, NotFoundError, ValidationError
from app.storage.keys import (
    assert_artifact_key_owned_by,
    assert_key_owned_by,
    build_artifact_key,
    build_key,
    safe_filename,
    sha256_hex,
    validate_tenant_id,
)
from app.storage.object_store import ObjectStore, StoredObject
from app.storage.validation import validate_upload

_ALLOWED = frozenset({"text/plain", "application/pdf"})


# ---------------------------------------------------------------------------
# Pure unit tests — key construction & the isolation seam (no mocks).
# ---------------------------------------------------------------------------


def test_build_key_is_tenant_prefixed_and_content_addressed() -> None:
    data = b"hello world"
    key = build_key("tenant-a", data, "report.pdf")

    assert key == f"tenant-a/{sha256_hex(data)}/report.pdf"
    # The tenant prefix is the leading segment (the seam, AC-2).
    assert key.split("/", 1)[0] == "tenant-a"


def test_identical_bytes_dedupe_to_same_key() -> None:
    # Content-addressing: same bytes => same middle segment regardless of name.
    a = build_key("t", b"same-bytes", "first.txt")
    b = build_key("t", b"same-bytes", "first.txt")
    assert a == b


def test_safe_filename_strips_paths_and_traversal() -> None:
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:\\Users\\x\\evil.pdf") == "evil.pdf"
    assert "/" not in safe_filename("a/b/c.txt")
    assert safe_filename("..hidden") == "hidden"
    # Always non-empty, even for a fully-stripped name.
    assert safe_filename("///") == "file"


def test_build_key_rejects_unsafe_tenant_id() -> None:
    # A tenant id that could forge a prefix or escape the namespace is refused.
    for bad in ["../other", "a/b", "", ".", "/abs"]:
        with pytest.raises(ValidationError):
            build_key(bad, b"x", "f.txt")


def test_validate_tenant_id_accepts_safe_values() -> None:
    assert validate_tenant_id("tenant-123") == "tenant-123"


def test_assert_key_owned_by_allows_same_prefix() -> None:
    # No raise => owned.
    assert_key_owned_by("tenant-a/abc/f.txt", "tenant-a")


def test_assert_key_owned_by_refuses_cross_prefix() -> None:
    with pytest.raises(ForbiddenError):
        assert_key_owned_by("tenant-b/abc/f.txt", "tenant-a")


def test_assert_key_owned_by_refuses_prefix_confusion() -> None:
    # "tenant-a" must not match a sibling whose name merely starts the same.
    with pytest.raises(ForbiddenError):
        assert_key_owned_by("tenant-ab/abc/f.txt", "tenant-a")


# ---------------------------------------------------------------------------
# Pure unit tests — artifact keys (issue #208, AC-5). The tenant segment sits
# after the ``artifacts/`` namespace prefix; the seam must key off that.
# ---------------------------------------------------------------------------


def test_build_artifact_key_is_namespaced_tenant_prefixed_and_content_addressed() -> None:
    data = b"agent output"
    key = build_artifact_key("tenant-a", data, "chart.png")
    assert key == f"artifacts/tenant-a/{sha256_hex(data)}/chart.png"
    # Distinct namespace from an upload of the same bytes/name.
    assert key != build_key("tenant-a", data, "chart.png")


def test_build_artifact_key_rejects_unsafe_tenant_id() -> None:
    for bad in ["../other", "a/b", "", ".", "/abs"]:
        with pytest.raises(ValidationError):
            build_artifact_key(bad, b"x", "f.csv")


def test_build_artifact_key_sanitizes_filename() -> None:
    key = build_artifact_key("t", b"x", "../../etc/passwd")
    assert key.endswith("/passwd")
    assert ".." not in key


def test_assert_artifact_key_owned_by_allows_same_prefix() -> None:
    # No raise => owned (the tenant segment is the second path component).
    assert_artifact_key_owned_by("artifacts/tenant-a/abc/f.csv", "tenant-a")


def test_assert_artifact_key_owned_by_refuses_cross_tenant() -> None:
    # AC-5 (negative): a forged artifact key under another tenant is refused.
    with pytest.raises(ForbiddenError):
        assert_artifact_key_owned_by("artifacts/tenant-b/abc/secret.csv", "tenant-a")


def test_assert_artifact_key_owned_by_refuses_missing_namespace() -> None:
    # An upload-style key (no ``artifacts/`` prefix) can never pass as an artifact —
    # the two namespaces are not interchangeable.
    with pytest.raises(ForbiddenError):
        assert_artifact_key_owned_by("tenant-a/abc/f.csv", "tenant-a")


def test_assert_artifact_key_owned_by_refuses_prefix_confusion() -> None:
    # "tenant-a" must not match a sibling whose name merely starts the same.
    with pytest.raises(ForbiddenError):
        assert_artifact_key_owned_by("artifacts/tenant-ab/abc/f.csv", "tenant-a")


# ---------------------------------------------------------------------------
# Pure unit tests — upload validation (AC-4 / AC-6).
# ---------------------------------------------------------------------------


def test_validate_upload_accepts_allowed_type_within_limit() -> None:
    validate_upload(
        size_bytes=10,
        content_type="text/plain",
        allowed_content_types=_ALLOWED,
        max_bytes=100,
    )  # no raise


def test_validate_upload_accepts_type_with_parameters() -> None:
    validate_upload(
        size_bytes=10,
        content_type="text/plain; charset=utf-8",
        allowed_content_types=_ALLOWED,
        max_bytes=100,
    )  # no raise


def test_validate_upload_rejects_disallowed_type() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_upload(
            size_bytes=10,
            content_type="application/x-msdownload",
            allowed_content_types=_ALLOWED,
            max_bytes=100,
        )
    assert exc.value.status == 422
    assert exc.value.code == "content_type_not_allowed"


def test_validate_upload_rejects_over_limit() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_upload(
            size_bytes=101,
            content_type="text/plain",
            allowed_content_types=_ALLOWED,
            max_bytes=100,
        )
    assert exc.value.code == "upload_too_large"


def test_validate_upload_rejects_empty() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_upload(
            size_bytes=0,
            content_type="text/plain",
            allowed_content_types=_ALLOWED,
            max_bytes=100,
        )
    assert exc.value.code == "empty_upload"


# ---------------------------------------------------------------------------
# Adapter unit tests — mocked S3 client.
# ---------------------------------------------------------------------------


def _unit_settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql+asyncpg://t:t@localhost:5432/t",
        REDIS_URL="redis://localhost:6379/0",
        CELERY_BROKER_URL="redis://localhost:6379/1",
        CELERY_RESULT_BACKEND="redis://localhost:6379/2",
        S3_ENDPOINT_URL="http://localhost:9000",
        S3_ACCESS_KEY="key",
        S3_SECRET_KEY="secret",
        S3_BUCKET="unit-bucket",
        UPLOAD_ALLOWED_CONTENT_TYPES="text/plain,application/pdf",
        MAX_UPLOAD_BYTES=100,
        S3_PRESIGN_TTL_SECONDS=120,
        # Artifact allowlist/cap (issue #208) — broader types + its own cap so the
        # adapter's artifact methods validate against the right config.
        ARTIFACT_ALLOWED_CONTENT_TYPES="text/csv,application/json,image/png",
        MAX_ARTIFACT_BYTES=200,
    )  # type: ignore[call-arg]


def _install_mock_client(store: ObjectStore, client: MagicMock) -> None:
    """Make ``store._client()`` yield ``client`` from an async context manager."""

    @contextlib.asynccontextmanager
    async def _cm() -> AsyncIterator[MagicMock]:
        yield client

    store._client = _cm  # type: ignore[method-assign]


def _streaming_body(data: bytes) -> MagicMock:
    """A mock of the aioboto3 streaming body (async context manager + read())."""
    body = MagicMock()
    body.__aenter__ = AsyncMock(return_value=body)
    body.__aexit__ = AsyncMock(return_value=False)
    body.read = AsyncMock(return_value=data)
    return body


@pytest.fixture
def store() -> ObjectStore:
    return ObjectStore(_unit_settings())


async def test_put_validates_then_stores(store: ObjectStore) -> None:
    client = MagicMock()
    client.put_object = AsyncMock()
    _install_mock_client(store, client)

    data = b"a report body"
    result = await store.put("tenant-a", data, "text/plain", "notes.txt")

    assert isinstance(result, StoredObject)
    assert result.key == f"tenant-a/{sha256_hex(data)}/notes.txt"
    assert result.sha256 == sha256_hex(data)
    assert result.size_bytes == len(data)
    client.put_object.assert_awaited_once()
    kwargs = client.put_object.await_args.kwargs
    assert kwargs["Bucket"] == "unit-bucket"
    assert kwargs["Key"] == result.key
    assert kwargs["Body"] == data
    assert kwargs["ContentType"] == "text/plain"


async def test_put_rejects_disallowed_type_before_io(store: ObjectStore) -> None:
    client = MagicMock()
    client.put_object = AsyncMock()
    _install_mock_client(store, client)

    with pytest.raises(ValidationError):
        await store.put("tenant-a", b"x", "application/zip", "a.zip")
    # AC-6: nothing was stored — the rejection happened before any write.
    client.put_object.assert_not_awaited()


async def test_put_rejects_over_limit_before_io(store: ObjectStore) -> None:
    client = MagicMock()
    client.put_object = AsyncMock()
    _install_mock_client(store, client)

    with pytest.raises(ValidationError):
        await store.put("tenant-a", b"x" * 101, "text/plain", "big.txt")
    client.put_object.assert_not_awaited()


async def test_get_round_trips_for_owning_tenant(store: ObjectStore) -> None:
    data = b"stored bytes"
    key = build_key("tenant-a", data, "notes.txt")
    client = MagicMock()
    client.get_object = AsyncMock(return_value={"Body": _streaming_body(data)})
    _install_mock_client(store, client)

    assert await store.get("tenant-a", key) == data
    client.get_object.assert_awaited_once_with(Bucket="unit-bucket", Key=key)


async def test_get_refuses_cross_tenant_before_io(store: ObjectStore) -> None:
    client = MagicMock()
    client.get_object = AsyncMock()
    _install_mock_client(store, client)

    # AC-5: a key under tenant-b is refused for tenant-a, before any I/O.
    with pytest.raises(ForbiddenError):
        await store.get("tenant-a", "tenant-b/abc/secret.txt")
    client.get_object.assert_not_awaited()


async def test_get_missing_object_maps_to_not_found(store: ObjectStore) -> None:
    from botocore.exceptions import ClientError

    key = build_key("tenant-a", b"x", "gone.txt")
    client = MagicMock()
    client.get_object = AsyncMock(
        side_effect=ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    )
    _install_mock_client(store, client)

    with pytest.raises(NotFoundError):
        await store.get("tenant-a", key)


async def test_delete_refuses_cross_tenant_before_io(store: ObjectStore) -> None:
    client = MagicMock()
    client.delete_object = AsyncMock()
    _install_mock_client(store, client)

    with pytest.raises(ForbiddenError):
        await store.delete("tenant-a", "tenant-b/abc/secret.txt")
    client.delete_object.assert_not_awaited()


async def test_delete_calls_client_for_owning_tenant(store: ObjectStore) -> None:
    key = build_key("tenant-a", b"x", "f.txt")
    client = MagicMock()
    client.delete_object = AsyncMock()
    _install_mock_client(store, client)

    await store.delete("tenant-a", key)
    client.delete_object.assert_awaited_once_with(Bucket="unit-bucket", Key=key)


async def test_presign_put_returns_key_and_url_with_ttl(store: ObjectStore) -> None:
    client = MagicMock()
    client.generate_presigned_url = AsyncMock(return_value="https://minio/presigned-put")
    _install_mock_client(store, client)

    data = b"a report body"
    key, url = await store.presign_put("tenant-a", data, "text/plain", "notes.txt")

    assert key == f"tenant-a/{sha256_hex(data)}/notes.txt"
    assert url == "https://minio/presigned-put"
    call = client.generate_presigned_url.await_args
    assert call.args[0] == "put_object"
    assert call.kwargs["Params"]["Key"] == key
    assert call.kwargs["Params"]["ContentType"] == "text/plain"
    assert call.kwargs["ExpiresIn"] == 120  # config-driven TTL (AC-3)


async def test_presign_put_validates_before_minting_url(store: ObjectStore) -> None:
    client = MagicMock()
    client.generate_presigned_url = AsyncMock()
    _install_mock_client(store, client)

    with pytest.raises(ValidationError):
        await store.presign_put("tenant-a", b"x", "application/zip", "a.zip")
    client.generate_presigned_url.assert_not_awaited()


async def test_presign_get_refuses_cross_tenant_before_io(store: ObjectStore) -> None:
    client = MagicMock()
    client.generate_presigned_url = AsyncMock()
    _install_mock_client(store, client)

    # AC-5: no presigned URL may ever be minted for another tenant's object.
    with pytest.raises(ForbiddenError):
        await store.presign_get("tenant-a", "tenant-b/abc/secret.txt")
    client.generate_presigned_url.assert_not_awaited()


async def test_presign_get_returns_url_for_owning_tenant(store: ObjectStore) -> None:
    key = build_key("tenant-a", b"x", "f.txt")
    client = MagicMock()
    client.generate_presigned_url = AsyncMock(return_value="https://minio/presigned-get")
    _install_mock_client(store, client)

    url = await store.presign_get("tenant-a", key)
    assert url == "https://minio/presigned-get"
    call = client.generate_presigned_url.await_args
    assert call.args[0] == "get_object"
    assert call.kwargs["ExpiresIn"] == 120


# ---------------------------------------------------------------------------
# Adapter unit tests — artifact methods (issue #208), mocked S3 client.
# ---------------------------------------------------------------------------


async def test_put_artifact_validates_then_stores_under_namespace(store: ObjectStore) -> None:
    client = MagicMock()
    client.put_object = AsyncMock()
    _install_mock_client(store, client)

    data = b'{"rows": 3}'
    result = await store.put_artifact("tenant-a", data, "application/json", "out.json")

    assert isinstance(result, StoredObject)
    assert result.key == f"artifacts/tenant-a/{sha256_hex(data)}/out.json"
    assert result.sha256 == sha256_hex(data)
    client.put_object.assert_awaited_once()
    assert client.put_object.await_args.kwargs["Key"] == result.key


async def test_put_artifact_rejects_disallowed_type_before_io(store: ObjectStore) -> None:
    client = MagicMock()
    client.put_object = AsyncMock()
    _install_mock_client(store, client)

    # application/pdf is an *upload* type but NOT an artifact type — must be refused.
    with pytest.raises(ValidationError):
        await store.put_artifact("tenant-a", b"x", "application/pdf", "a.pdf")
    client.put_object.assert_not_awaited()


async def test_put_artifact_rejects_over_artifact_cap_before_io(store: ObjectStore) -> None:
    client = MagicMock()
    client.put_object = AsyncMock()
    _install_mock_client(store, client)

    # 201 bytes exceeds MAX_ARTIFACT_BYTES=200 (distinct from the upload cap).
    with pytest.raises(ValidationError):
        await store.put_artifact("tenant-a", b"x" * 201, "text/csv", "big.csv")
    client.put_object.assert_not_awaited()


async def test_get_artifact_round_trips_for_owning_tenant(store: ObjectStore) -> None:
    data = b"col1,col2\n1,2\n"
    key = build_artifact_key("tenant-a", data, "data.csv")
    client = MagicMock()
    client.get_object = AsyncMock(return_value={"Body": _streaming_body(data)})
    _install_mock_client(store, client)

    assert await store.get_artifact("tenant-a", key) == data
    client.get_object.assert_awaited_once_with(Bucket="unit-bucket", Key=key)


async def test_get_artifact_refuses_cross_tenant_before_io(store: ObjectStore) -> None:
    client = MagicMock()
    client.get_object = AsyncMock()
    _install_mock_client(store, client)

    # AC-5: a forged artifact key under tenant-b is refused for tenant-a, before I/O.
    with pytest.raises(ForbiddenError):
        await store.get_artifact("tenant-a", "artifacts/tenant-b/abc/secret.csv")
    client.get_object.assert_not_awaited()


async def test_delete_artifact_refuses_cross_tenant_before_io(store: ObjectStore) -> None:
    client = MagicMock()
    client.delete_object = AsyncMock()
    _install_mock_client(store, client)

    with pytest.raises(ForbiddenError):
        await store.delete_artifact("tenant-a", "artifacts/tenant-b/abc/secret.csv")
    client.delete_object.assert_not_awaited()


async def test_presign_get_artifact_refuses_cross_tenant_before_io(store: ObjectStore) -> None:
    client = MagicMock()
    client.generate_presigned_url = AsyncMock()
    _install_mock_client(store, client)

    with pytest.raises(ForbiddenError):
        await store.presign_get_artifact("tenant-a", "artifacts/tenant-b/abc/secret.csv")
    client.generate_presigned_url.assert_not_awaited()


async def test_presign_get_artifact_returns_url_for_owning_tenant(store: ObjectStore) -> None:
    key = build_artifact_key("tenant-a", b"x", "f.csv")
    client = MagicMock()
    client.generate_presigned_url = AsyncMock(return_value="https://minio/presigned-artifact")
    _install_mock_client(store, client)

    url = await store.presign_get_artifact("tenant-a", key)
    assert url == "https://minio/presigned-artifact"
    call = client.generate_presigned_url.await_args
    assert call.args[0] == "get_object"
    assert call.kwargs["ExpiresIn"] == 120


# ---------------------------------------------------------------------------
# Integration — live compose MinIO round-trip (AC-1). Skipped if unreachable.
# ---------------------------------------------------------------------------


def _minio_reachable(endpoint: str) -> bool:
    parsed = urlparse(endpoint)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


_INTEGRATION_ENDPOINT = os.environ.get("S3_ENDPOINT_URL", "http://localhost:47184")
_integration = pytest.mark.skipif(
    not _minio_reachable(_INTEGRATION_ENDPOINT),
    reason=(
        f"MinIO not reachable at {_INTEGRATION_ENDPOINT}; "
        "integration test skipped (offline-safe)."
    ),
)


def _integration_settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql+asyncpg://t:t@localhost:5432/t",
        REDIS_URL="redis://localhost:6379/0",
        CELERY_BROKER_URL="redis://localhost:6379/1",
        CELERY_RESULT_BACKEND="redis://localhost:6379/2",
        S3_ENDPOINT_URL=_INTEGRATION_ENDPOINT,
        S3_ACCESS_KEY=os.environ.get("S3_ACCESS_KEY", "lumen"),
        S3_SECRET_KEY=os.environ.get("S3_SECRET_KEY", "lumen_local_dev_secret"),
        S3_BUCKET=os.environ.get("S3_BUCKET", "lumen-uploads"),
    )  # type: ignore[call-arg]


@pytest.fixture
def integration_store() -> Iterator[ObjectStore]:
    yield ObjectStore(_integration_settings())


@_integration
async def test_minio_put_get_round_trip(integration_store: ObjectStore) -> None:
    store = integration_store
    await store.ensure_bucket()

    # Unique tenant per run so concurrent/repeat runs never collide; teardown
    # below removes the object regardless of test outcome.
    tenant = f"itest-{uuid.uuid4().hex[:12]}"
    payload = f"round-trip {uuid.uuid4()}".encode()

    stored: StoredObject | None = None
    try:
        stored = await store.put(tenant, payload, "text/plain", "round-trip.txt")
        # AC-1: bytes come back identically.
        assert await store.get(tenant, stored.key) == payload
        # AC-2: the live key is tenant-prefixed + content-addressed.
        assert stored.key == f"{tenant}/{sha256_hex(payload)}/round-trip.txt"
        # AC-5 against a live backend: another tenant cannot read this key.
        with pytest.raises(ForbiddenError):
            await store.get("itest-other-tenant", stored.key)
        # AC-3: a presigned GET URL is minted and points at the bucket/key.
        url = await store.presign_get(tenant, stored.key)
        assert stored.key in url
    finally:
        if stored is not None:
            await store.delete(tenant, stored.key)
            # Teardown verified: the object is gone.
            with pytest.raises(NotFoundError):
                await store.get(tenant, stored.key)


@_integration
async def test_minio_rejects_disallowed_type_without_storing(
    integration_store: ObjectStore,
) -> None:
    # AC-6 against a live backend: a disallowed type never reaches storage.
    tenant = f"itest-{uuid.uuid4().hex[:12]}"
    with pytest.raises(ValidationError):
        await integration_store.put(tenant, b"x", "application/zip", "a.zip")
