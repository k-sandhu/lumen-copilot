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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlparse

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from httpx import AsyncClient

from app.core.config import Settings
from app.core.errors import DependencyError, ForbiddenError, NotFoundError, ValidationError
from app.storage.keys import (
    assert_artifact_key_owned_by,
    assert_key_owned_by,
    build_artifact_key,
    build_avatar_key,
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


def test_build_avatar_key_is_tenant_and_user_prefixed_and_content_addressed() -> None:
    data = b"avatar bytes"
    key = build_avatar_key("tenant-a", "user-1", data, "me.png")
    # {tenant_id}/{user_id}/{sha}/{name} — the tenant prefix is the isolation seam,
    # the user_id segment scopes it to the owning user.
    assert key == f"tenant-a/user-1/{sha256_hex(data)}/me.png"
    # The generic tenant-prefix seam still applies (retrieval reuses presign_get).
    assert_key_owned_by(key, "tenant-a")
    with pytest.raises(ForbiddenError):
        assert_key_owned_by(key, "tenant-b")


def test_build_avatar_key_rejects_unsafe_tenant_or_user_id() -> None:
    for bad in ["../other", "a/b", "", ".", "/abs"]:
        with pytest.raises(ValidationError):
            build_avatar_key(bad, "user-1", b"x", "f.png")
        with pytest.raises(ValidationError):
            build_avatar_key("tenant-a", bad, b"x", "f.png")


def test_build_avatar_key_sanitizes_filename() -> None:
    key = build_avatar_key("t", "u", b"x", "../../etc/passwd")
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


def _unit_settings(**overrides: object) -> Settings:
    kwargs: dict[str, object] = {
        "DATABASE_URL": "postgresql+asyncpg://t:t@localhost:5432/t",
        "REDIS_URL": "redis://localhost:6379/0",
        "CELERY_BROKER_URL": "redis://localhost:6379/1",
        "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_ACCESS_KEY": "key",
        "S3_SECRET_KEY": "secret",
        "S3_BUCKET": "unit-bucket",
        "UPLOAD_ALLOWED_CONTENT_TYPES": "text/plain,application/pdf",
        "MAX_UPLOAD_BYTES": 100,
        "S3_PRESIGN_TTL_SECONDS": 120,
        # Artifact allowlist/cap (issue #208) — broader types + its own cap so the
        # adapter's artifact methods validate against the right config.
        "ARTIFACT_ALLOWED_CONTENT_TYPES": "text/csv,application/json,image/png",
        "MAX_ARTIFACT_BYTES": 200,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)  # type: ignore[arg-type]


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


async def test_ensure_bucket_merges_named_incomplete_multipart_lifecycle(
    store: ObjectStore,
) -> None:
    client = MagicMock()
    client.head_bucket = AsyncMock()
    client.put_bucket_cors = AsyncMock()
    unrelated = {
        "ID": "retain-reports",
        "Status": "Enabled",
        "Filter": {"Prefix": "reports/"},
        "Expiration": {"Days": 30},
    }
    client.get_bucket_lifecycle_configuration = AsyncMock(return_value={"Rules": [unrelated]})
    client.put_bucket_lifecycle_configuration = AsyncMock()
    _install_mock_client(store, client)

    await store.ensure_bucket()

    lifecycle = client.put_bucket_lifecycle_configuration.await_args.kwargs[
        "LifecycleConfiguration"
    ]
    assert lifecycle["Rules"][0] == unrelated
    assert lifecycle["Rules"][1] == {
        "ID": "lumen-abort-incomplete-multipart",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 2},
    }


async def test_ensure_bucket_upserts_only_lumen_lifecycle_rule(store: ObjectStore) -> None:
    client = MagicMock()
    client.head_bucket = AsyncMock()
    client.put_bucket_cors = AsyncMock()
    unrelated = {
        "ID": "retain-reports",
        "Status": "Enabled",
        "Filter": {"Prefix": "reports/"},
        "Expiration": {"Days": 30},
    }
    stale_lumen = {
        "ID": "lumen-abort-incomplete-multipart",
        "Status": "Disabled",
        "Filter": {"Prefix": ""},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 9},
    }
    client.get_bucket_lifecycle_configuration = AsyncMock(
        return_value={"Rules": [stale_lumen, unrelated]}
    )
    client.put_bucket_lifecycle_configuration = AsyncMock()
    _install_mock_client(store, client)

    await store.ensure_bucket()

    rules = client.put_bucket_lifecycle_configuration.await_args.kwargs["LifecycleConfiguration"][
        "Rules"
    ]
    assert rules[0] == unrelated
    assert rules[1]["ID"] == "lumen-abort-incomplete-multipart"
    assert rules[1]["Status"] == "Enabled"
    assert rules[1]["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 2}


async def test_externally_managed_cors_skips_bucket_api_but_keeps_lifecycle_and_readiness() -> None:
    """OSS MinIO uses its process-level CORS setting, not PutBucketCors."""
    store = ObjectStore(_unit_settings(S3_CORS_MANAGED_EXTERNALLY=True))
    client = MagicMock()
    client.head_bucket = AsyncMock()
    client.put_bucket_cors = AsyncMock()
    client.get_bucket_lifecycle_configuration = AsyncMock(
        side_effect=ClientError(
            {"Error": {"Code": "NoSuchLifecycleConfiguration"}},
            "GetBucketLifecycleConfiguration",
        )
    )
    client.put_bucket_lifecycle_configuration = AsyncMock()
    client.list_buckets = AsyncMock()
    _install_mock_client(store, client)

    await store.ensure_bucket()
    await store.ping()

    client.put_bucket_cors.assert_not_awaited()
    client.put_bucket_lifecycle_configuration.assert_awaited_once()
    client.list_buckets.assert_awaited_once()


async def test_externally_managed_minio_controls_skip_unsupported_bucket_apis() -> None:
    """Compose supplies MinIO CORS plus a bounded ``mc`` multipart reaper."""
    store = ObjectStore(
        _unit_settings(
            S3_CORS_MANAGED_EXTERNALLY=True,
            S3_INCOMPLETE_MULTIPART_CLEANUP_MANAGED_EXTERNALLY=True,
        )
    )
    client = MagicMock()
    client.head_bucket = AsyncMock()
    client.put_bucket_cors = AsyncMock()
    client.get_bucket_lifecycle_configuration = AsyncMock()
    client.put_bucket_lifecycle_configuration = AsyncMock()
    client.list_buckets = AsyncMock()
    _install_mock_client(store, client)

    await store.ensure_bucket()
    await store.ping()

    client.put_bucket_cors.assert_not_awaited()
    client.get_bucket_lifecycle_configuration.assert_not_awaited()
    client.put_bucket_lifecycle_configuration.assert_not_awaited()
    client.list_buckets.assert_awaited_once()


async def test_ping_requires_successful_full_bucket_bootstrap(store: ObjectStore) -> None:
    client = MagicMock()
    client.list_buckets = AsyncMock()
    _install_mock_client(store, client)

    with pytest.raises(DependencyError) as exc_info:
        await store.ping()

    assert exc_info.value.code == "storage_bootstrap_incomplete"
    client.list_buckets.assert_not_awaited()


async def test_cors_bootstrap_failure_keeps_readiness_degraded(store: ObjectStore) -> None:
    client = MagicMock()
    client.head_bucket = AsyncMock()
    client.put_bucket_cors = AsyncMock(
        side_effect=ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "policy detail"}},
            "PutBucketCors",
        )
    )
    client.list_buckets = AsyncMock()
    _install_mock_client(store, client)

    with pytest.raises(DependencyError) as bootstrap_error:
        await store.ensure_bucket()
    assert bootstrap_error.value.code == "storage_bootstrap_failed"
    assert "policy detail" not in str(bootstrap_error.value)

    with pytest.raises(DependencyError) as readiness_error:
        await store.ping()
    assert readiness_error.value.code == "storage_bootstrap_incomplete"
    client.list_buckets.assert_not_awaited()


async def test_ping_reaches_provider_after_successful_bootstrap(store: ObjectStore) -> None:
    client = MagicMock()
    client.head_bucket = AsyncMock()
    client.put_bucket_cors = AsyncMock()
    client.get_bucket_lifecycle_configuration = AsyncMock(
        side_effect=ClientError(
            {"Error": {"Code": "NoSuchLifecycleConfiguration"}},
            "GetBucketLifecycleConfiguration",
        )
    )
    client.put_bucket_lifecycle_configuration = AsyncMock()
    client.list_buckets = AsyncMock()
    _install_mock_client(store, client)

    await store.ensure_bucket()
    await store.ping()

    client.list_buckets.assert_awaited_once()


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


async def test_download_to_path_streams_and_returns_metadata(
    store: ObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = build_key("tenant-a", b"streamed", "media.mp3")
    body = _streaming_body(b"")
    body.read = AsyncMock(side_effect=[b"stre", b"amed", b""])
    client = MagicMock()
    client.get_object = AsyncMock(
        return_value={
            "Body": body,
            "ContentLength": 8,
            "ContentType": "audio/mpeg",
            "Metadata": {"lumen-document-id": "doc-1"},
        }
    )
    _install_mock_client(store, client)
    written = bytearray()

    class _Output:
        async def __aenter__(self) -> _Output:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def write(self, chunk: bytes) -> None:
            written.extend(chunk)

    async def open_output(destination: Path, mode: str) -> _Output:
        assert destination == Path("media.mp3")
        assert mode == "wb"
        return _Output()

    monkeypatch.setattr("app.storage.object_store.open_file", open_output)

    metadata = await store.download_to_path("tenant-a", key, Path("media.mp3"))

    assert bytes(written) == b"streamed"
    assert metadata.size_bytes == 8
    assert metadata.content_type == "audio/mpeg"
    assert metadata.metadata == {"lumen-document-id": "doc-1"}


async def test_download_to_path_maps_vendor_failure_to_typed_dependency(
    store: ObjectStore,
) -> None:
    from botocore.exceptions import ClientError

    key = build_key("tenant-a", b"x", "media.mp3")
    client = MagicMock()
    client.get_object = AsyncMock(
        side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
    )
    _install_mock_client(store, client)

    with pytest.raises(DependencyError) as exc_info:
        await store.download_to_path("tenant-a", key, Path("media.mp3"))
    assert exc_info.value.code == "storage_unavailable"


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


@pytest.mark.parametrize("failure_kind", ["client", "transport"])
@pytest.mark.parametrize(
    "operation",
    [
        "create",
        "sign_part",
        "list_parts",
        "complete",
        "abort",
        "head",
        "access_url",
        "delete",
    ],
)
async def test_direct_transfer_provider_failures_are_opaque_dependencies(
    store: ObjectStore, operation: str, failure_kind: str
) -> None:
    failure: Exception
    if failure_kind == "client":
        failure = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "vendor secret"}},
            "S3Operation",
        )
    else:
        failure = EndpointConnectionError(endpoint_url="http://private-storage")
    client = MagicMock()
    method_name = {
        "create": "create_multipart_upload",
        "sign_part": "generate_presigned_url",
        "list_parts": "list_parts",
        "complete": "complete_multipart_upload",
        "abort": "abort_multipart_upload",
        "head": "head_object",
        "access_url": "generate_presigned_url",
        "delete": "delete_object",
    }[operation]
    setattr(client, method_name, AsyncMock(side_effect=failure))
    _install_mock_client(store, client)
    key = "tenant-a/quarantine/doc-1/meeting.mp3"

    with pytest.raises(DependencyError) as exc_info:
        if operation == "create":
            await store.create_multipart_upload(
                tenant_id="tenant-a",
                document_id="doc-1",
                upload_id="upload-1",
                filename="meeting.mp3",
                content_type="audio/mpeg",
            )
        elif operation == "sign_part":
            await store.presign_upload_part(
                tenant_id="tenant-a",
                key=key,
                provider_upload_id="provider-1",
                part_number=1,
            )
        elif operation == "list_parts":
            await store.list_multipart_parts(
                tenant_id="tenant-a",
                key=key,
                provider_upload_id="provider-1",
            )
        elif operation == "complete":
            await store.complete_multipart_upload(
                tenant_id="tenant-a",
                key=key,
                provider_upload_id="provider-1",
                parts=[(1, '"etag-1"')],
            )
        elif operation == "abort":
            await store.abort_multipart_upload(
                tenant_id="tenant-a",
                key=key,
                provider_upload_id="provider-1",
            )
        elif operation == "head":
            await store.head("tenant-a", key)
        elif operation == "access_url":
            await store.presign_get("tenant-a", key)
        else:
            await store.delete("tenant-a", key)

    assert exc_info.value.code == "storage_unavailable"
    assert "vendor secret" not in str(exc_info.value)


@pytest.mark.parametrize("operation", ["list", "complete", "abort"])
async def test_no_such_multipart_upload_preserves_terminal_semantics(
    store: ObjectStore, operation: str
) -> None:
    client = MagicMock()
    failure = ClientError({"Error": {"Code": "NoSuchUpload", "Message": "gone"}}, "Multipart")
    method_name = {
        "list": "list_parts",
        "complete": "complete_multipart_upload",
        "abort": "abort_multipart_upload",
    }[operation]
    setattr(client, method_name, AsyncMock(side_effect=failure))
    _install_mock_client(store, client)
    kwargs = {
        "tenant_id": "tenant-a",
        "key": "tenant-a/quarantine/doc-1/meeting.mp3",
        "provider_upload_id": "provider-1",
    }

    if operation == "abort":
        await store.abort_multipart_upload(**kwargs)
    elif operation == "list":
        with pytest.raises(NotFoundError) as exc_info:
            await store.list_multipart_parts(**kwargs)
        assert exc_info.value.code == "multipart_upload_not_found"
    else:
        with pytest.raises(NotFoundError) as exc_info:
            await store.complete_multipart_upload(**kwargs, parts=[(1, '"etag-1"')])
        assert exc_info.value.code == "multipart_upload_not_found"


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


# Presigned URLs are minted against the PUBLIC endpoint (issue #241).
#
# SigV4 binds the signature to the Host header, so a URL presigned against the
# in-network endpoint (http://minio:9000 inside compose) is unreachable AND
# unfixable from a browser — rewriting the host breaks the signature. When
# S3_PUBLIC_ENDPOINT_URL is set, presign_get/presign_put must sign against it;
# everything else (put/get/delete) stays on the internal endpoint. Presigning
# is pure client-side signing, so these tests use the REAL client — no mocks,
# no network.
# ---------------------------------------------------------------------------


async def test_presign_get_uses_public_endpoint_when_configured() -> None:
    store_public = ObjectStore(_unit_settings(S3_PUBLIC_ENDPOINT_URL="http://public.example:47184"))
    key = build_key("tenant-a", b"data", "f.txt")

    url = await store_public.presign_get("tenant-a", key)

    assert url.startswith("http://public.example:47184/")
    assert "unit-bucket" in url
    assert "X-Amz-Signature=" in url


async def test_presign_put_uses_public_endpoint_when_configured() -> None:
    store_public = ObjectStore(_unit_settings(S3_PUBLIC_ENDPOINT_URL="http://public.example:47184"))

    _key, url = await store_public.presign_put("tenant-a", b"data", "text/plain", "f.txt")

    assert url.startswith("http://public.example:47184/")


async def test_presign_get_defaults_to_internal_endpoint_when_unset() -> None:
    # No S3_PUBLIC_ENDPOINT_URL ⇒ single-network deployment: presign against
    # the (only) configured endpoint, exactly as before.
    store_internal = ObjectStore(_unit_settings())
    key = build_key("tenant-a", b"data", "f.txt")

    url = await store_internal.presign_get("tenant-a", key)

    assert url.startswith("http://localhost:9000/")


async def test_put_keeps_internal_endpoint_when_public_configured() -> None:
    # Only URL MINTING moves to the public endpoint; object I/O stays on the
    # in-network client (the API/worker cannot necessarily reach the public URL).
    store_public = ObjectStore(_unit_settings(S3_PUBLIC_ENDPOINT_URL="http://public.example:47184"))
    client = MagicMock()
    client.put_object = AsyncMock()
    _install_mock_client(store_public, client)

    await store_public.put("tenant-a", b"body", "text/plain", "notes.txt")

    client.put_object.assert_awaited_once()


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
_RUN_LIVE = os.environ.get("RUN_LIVE") == "1"
_integration = pytest.mark.skipif(
    not _RUN_LIVE,
    reason=(
        "live MinIO tests opted out: set RUN_LIVE=1 exactly "
        "(offline-safe; no socket is opened by default)."
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
        S3_PUBLIC_ENDPOINT_URL=os.environ.get("S3_PUBLIC_ENDPOINT_URL", _INTEGRATION_ENDPOINT),
        S3_CORS_ALLOWED_ORIGINS="http://localhost:47180",
        S3_INCOMPLETE_MULTIPART_CLEANUP_MANAGED_EXTERNALLY=os.environ.get(
            "S3_INCOMPLETE_MULTIPART_CLEANUP_MANAGED_EXTERNALLY", "false"
        ),
    )  # type: ignore[call-arg]


@pytest.fixture
def integration_store() -> Iterator[ObjectStore]:
    if not _minio_reachable(_INTEGRATION_ENDPOINT):
        pytest.skip(f"RUN_LIVE=1 but MinIO is not reachable at {_INTEGRATION_ENDPOINT}")
    yield ObjectStore(_integration_settings())


@pytest.mark.live
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


@pytest.mark.live
@_integration
async def test_minio_rejects_disallowed_type_without_storing(
    integration_store: ObjectStore,
) -> None:
    # AC-6 against a live backend: a disallowed type never reaches storage.
    tenant = f"itest-{uuid.uuid4().hex[:12]}"
    with pytest.raises(ValidationError):
        await integration_store.put(tenant, b"x", "application/zip", "a.zip")


@pytest.mark.live
@_integration
async def test_minio_direct_multipart_cors_etag_and_range_round_trip(
    integration_store: ObjectStore,
) -> None:
    """Spec 0008 §9 browser data-plane proof with unique state + teardown."""
    store = integration_store
    await store.ensure_bucket()
    tenant = f"itest-{uuid.uuid4().hex[:12]}"
    document_id = uuid.uuid4().hex
    upload_id = uuid.uuid4().hex
    origin = "http://localhost:47180"
    first_part = b"a" * (5 * 1024 * 1024)
    second_part = b"range-proof-571"
    multipart = await store.create_multipart_upload(
        tenant_id=tenant,
        document_id=document_id,
        upload_id=upload_id,
        filename="meeting.mp3",
        content_type="audio/mpeg",
    )
    completed = False
    try:
        etags: list[tuple[int, str]] = []
        async with AsyncClient(timeout=30.0) as client:
            for part_number, payload in enumerate((first_part, second_part), 1):
                url = await store.presign_upload_part(
                    tenant_id=tenant,
                    key=multipart.key,
                    provider_upload_id=multipart.provider_upload_id,
                    part_number=part_number,
                )
                preflight = await client.options(
                    url,
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": "PUT",
                    },
                )
                assert preflight.status_code in {200, 204}
                assert preflight.headers["access-control-allow-origin"] == origin
                assert "PUT" in preflight.headers["access-control-allow-methods"]

                uploaded = await client.put(url, content=payload, headers={"Origin": origin})
                assert uploaded.status_code == 200, uploaded.text
                assert uploaded.headers["access-control-allow-origin"] == origin
                assert "etag" in uploaded.headers["access-control-expose-headers"].lower()
                etags.append((part_number, uploaded.headers["etag"]))

            stored = await store.complete_multipart_upload(
                tenant_id=tenant,
                key=multipart.key,
                provider_upload_id=multipart.provider_upload_id,
                parts=etags,
            )
            completed = True
            assert stored.size_bytes == len(first_part) + len(second_part)

            read_url = await store.presign_get(tenant, multipart.key, content_type="audio/mpeg")
            ranged = await client.get(
                read_url,
                headers={"Origin": origin, "Range": "bytes=0-15"},
            )
            assert ranged.status_code == 206
            assert ranged.content == first_part[:16]
            assert ranged.headers["access-control-allow-origin"] == origin
            exposed = ranged.headers["access-control-expose-headers"].lower()
            assert "content-range" in exposed
            assert ranged.headers["content-range"].startswith("bytes 0-15/")
    finally:
        if not completed:
            await store.abort_multipart_upload(
                tenant_id=tenant,
                key=multipart.key,
                provider_upload_id=multipart.provider_upload_id,
            )
        await store.delete(tenant, multipart.key)
        with pytest.raises(NotFoundError):
            await store.head(tenant, multipart.key)
