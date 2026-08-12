"""S3 / MinIO adapter.

The **only** constructor of an S3 client (ADR-0004). Uses ``aioboto3`` so all
object-store I/O is async and never blocks the event loop. A fresh client is
created per operation via the session's async context manager (the documented
``aioboto3`` pattern); the session itself is cheap and reused.

The adapter addresses every object by a **tenant-prefixed** key
(:mod:`app.storage.keys`) and refuses any read whose key is outside the caller's
tenant prefix — the isolation **seam** for issue #22, ahead of the full ACL in
CC-1. Small legacy objects are content-addressed; direct multipart uploads use a
random document-scoped quarantine key until ingestion succeeds. Declared
content-type and size are validated against the config allowlist/limit before
any bytes are stored.

Callers see only domain types (``str`` keys, :class:`StoredObject`, ``bytes``),
never a ``botocore`` object (ADR-0004 adapter rule 1).

Exposes:
* :meth:`put` — validate, content-address, and store bytes; returns a
  :class:`StoredObject`.
* :meth:`get` — tenant-checked read-back of stored bytes.
* :meth:`delete` — tenant-checked removal of an object.
* :meth:`presign_put` / :meth:`presign_get` — short-TTL presigned URLs so large
  files transfer directly to/from storage, not through the API process.
* :meth:`put_logo` — store a per-tenant application logo (admin branding):
  the standard upload key shape, validated against the image-only logo
  allowlist/limit; retrieval reuses the generic :meth:`presign_get`.
* :meth:`put_avatar` — store a per-user profile avatar: the same image-only
  allowlist/limit as the logo (reused), keyed ``{tenant_id}/{user_id}/{sha}/{name}``
  so it is scoped to the owning user; retrieval reuses the generic :meth:`presign_get`.
* :meth:`ensure_bucket` — create ``S3_BUCKET`` if missing; called at startup so
  the bucket exists from day one.
* :meth:`ping` — a cheap reachability check used by the readiness probe.
* :meth:`put_artifact` / :meth:`get_artifact` / :meth:`delete_artifact` /
  :meth:`presign_get_artifact` — the parallel lifecycle for **agent/run-produced
  artifacts** (issue #208): keys namespaced under ``artifacts/`` and validated
  against the broader *artifact* allowlist/limit, the tenant-prefix seam enforced
  against ``artifacts/{tenant_id}/``.

Scope note: DLP/malware scanning, KMS/SSE, and the parsing sandbox are fenced
**OUT** of #22 (OD-4 data invariants / CC-5 #21). Artifact **retention** (a
janitor purging past ``retention_expires_at``) is #208's, stubbed in
:mod:`app.tasks.artifact_retention`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aioboto3
from anyio import open_file
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings
from app.core.errors import DependencyError, NotFoundError
from app.storage.keys import (
    assert_artifact_key_owned_by,
    assert_key_owned_by,
    build_artifact_key,
    build_avatar_key,
    build_key,
    build_quarantine_key,
    sha256_hex,
)
from app.storage.validation import validate_upload


@dataclass(frozen=True, slots=True)
class StoredObject:
    """The result of a :meth:`ObjectStore.put` — a domain value, no vendor type.

    Attributes:
        key: the tenant-prefixed, content-addressed object key.
        sha256: hex content address of the stored bytes.
        size_bytes: number of bytes stored.
        content_type: the (allowlisted) declared content-type.
    """

    key: str
    sha256: str
    size_bytes: int
    content_type: str


@dataclass(frozen=True, slots=True)
class MultipartUpload:
    key: str
    provider_upload_id: str


@dataclass(frozen=True, slots=True)
class UploadedPart:
    part_number: int
    etag: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class StoredObjectMetadata:
    key: str
    size_bytes: int
    content_type: str
    metadata: dict[str, str]


class ObjectStore:
    """Async S3/MinIO adapter scoped to the configured bucket."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._bucket = settings.s3_bucket
        self._session = aioboto3.Session()
        # Path-style addressing + signature v4 work against MinIO and S3 alike.
        self._client_config = BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 2, "mode": "standard"},
        )
        # Readiness means more than reachability: direct browser uploads are
        # safe only after bucket creation and every non-external CORS / orphaned
        # multipart cleanup mechanism has succeeded in this process.
        self._bootstrap_ready = False

    def _client(self) -> Any:
        """Return an async client context manager for the object store."""
        return self._session.client(
            "s3",
            endpoint_url=self._settings.s3_endpoint_url,
            aws_access_key_id=self._settings.s3_access_key,
            aws_secret_access_key=self._settings.s3_secret_key,
            config=self._client_config,
        )

    def _presign_client(self) -> Any:
        """Client used only to MINT presigned URLs (#241).

        Presigning is pure client-side signing (no network I/O), but SigV4
        binds the signature to the Host header — a URL presigned against the
        in-network endpoint (``http://minio:9000`` inside compose) is both
        unreachable from a browser and unfixable after the fact (rewriting the
        host invalidates the signature). When ``S3_PUBLIC_ENDPOINT_URL`` is set,
        sign against it; otherwise fall back to the internal client. Object
        I/O (``put``/``get``/``delete``) always stays on the internal endpoint.
        """
        public = self._settings.s3_public_endpoint_url
        if public is None:
            return self._client()
        return self._session.client(
            "s3",
            endpoint_url=public,
            aws_access_key_id=self._settings.s3_access_key,
            aws_secret_access_key=self._settings.s3_secret_key,
            config=self._client_config,
        )

    # --- Bootstrap / health ---------------------------------------------------

    async def ensure_bucket(self) -> None:
        """Create the configured bucket if it does not already exist.

        Idempotent: a pre-existing bucket is a no-op. Called from the app
        lifespan so uploads have a target from first boot.
        """
        self._bootstrap_ready = False
        try:
            async with self._client() as client:
                try:
                    await client.head_bucket(Bucket=self._bucket)
                except ClientError as exc:
                    code = exc.response.get("Error", {}).get("Code", "")
                    if code in {"404", "NoSuchBucket", "NotFound"}:
                        await client.create_bucket(Bucket=self._bucket)
                    else:
                        raise
                if (
                    self._settings.s3_cors_allowed_origins
                    and not self._settings.s3_cors_managed_externally
                ):
                    await client.put_bucket_cors(
                        Bucket=self._bucket,
                        CORSConfiguration={
                            "CORSRules": [
                                {
                                    "AllowedOrigins": sorted(
                                        self._settings.s3_cors_allowed_origins
                                    ),
                                    "AllowedMethods": ["PUT", "GET", "HEAD"],
                                    "AllowedHeaders": ["*"],
                                    "ExposeHeaders": [
                                        "ETag",
                                        "Content-Length",
                                        "Content-Range",
                                        "Accept-Ranges",
                                    ],
                                    "MaxAgeSeconds": 3600,
                                }
                            ]
                        },
                    )
                if not self._settings.s3_incomplete_multipart_cleanup_managed_externally:
                    await self._ensure_incomplete_multipart_lifecycle(client)
        except (ClientError, BotoCoreError) as exc:
            raise DependencyError(
                "Object storage bootstrap failed.", code="storage_bootstrap_failed"
            ) from exc
        self._bootstrap_ready = True

    async def _ensure_incomplete_multipart_lifecycle(self, client: Any) -> None:
        """Merge Lumen's abort backstop without replacing unrelated S3 rules."""
        rule_id = "lumen-abort-incomplete-multipart"
        desired: dict[str, object] = {
            "ID": rule_id,
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "AbortIncompleteMultipartUpload": {
                "DaysAfterInitiation": self._settings.upload_incomplete_lifecycle_days
            },
        }
        try:
            response = await client.get_bucket_lifecycle_configuration(Bucket=self._bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in {
                "NoSuchLifecycleConfiguration",
                "NoSuchLifecycle",
                "404",
                "NotFound",
            }:
                raise
            rules: list[dict[str, object]] = []
        else:
            rules = [dict(rule) for rule in response.get("Rules", [])]

        existing = next((rule for rule in rules if rule.get("ID") == rule_id), None)
        if existing == desired:
            return
        merged = [rule for rule in rules if rule.get("ID") != rule_id]
        merged.append(desired)
        await client.put_bucket_lifecycle_configuration(
            Bucket=self._bucket,
            LifecycleConfiguration={"Rules": merged},
        )

    async def ping(self) -> None:
        """Check both successful bootstrap and current provider reachability."""
        if not self._bootstrap_ready:
            raise DependencyError(
                "Object storage bootstrap is incomplete.",
                code="storage_bootstrap_incomplete",
            )
        try:
            async with self._client() as client:
                await client.list_buckets()
        except (ClientError, BotoCoreError) as exc:
            raise DependencyError(
                "Object storage is unavailable.", code="storage_unavailable"
            ) from exc

    # --- Object lifecycle -----------------------------------------------------

    async def put(
        self,
        tenant_id: str,
        data: bytes,
        content_type: str,
        filename: str,
    ) -> StoredObject:
        """Validate, content-address, and store ``data``; return its descriptor.

        The declared ``content_type`` and the byte length are validated against
        the config allowlist/limit **before** any write (AC-4/AC-6); a rejection
        is a typed ``ValidationError`` (→ 4xx). The key is
        ``{tenant_id}/{sha256(data)}/{safe_filename}`` (AC-2), so identical bytes
        dedupe and every object is tenant-scoped.
        """
        validate_upload(
            size_bytes=len(data),
            content_type=content_type,
            allowed_content_types=self._settings.upload_allowed_content_types,
            max_bytes=self._settings.max_upload_bytes,
        )
        key = build_key(tenant_id, data, filename)
        async with self._client() as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        return StoredObject(
            key=key,
            sha256=sha256_hex(data),
            size_bytes=len(data),
            content_type=content_type,
        )

    async def get(self, tenant_id: str, key: str) -> bytes:
        """Read back the bytes at ``key`` for ``tenant_id``.

        Enforces the tenant-prefix seam first (AC-5): a key outside the caller's
        ``{tenant_id}/`` prefix is refused with ``ForbiddenError`` before any I/O,
        so cross-tenant reads are impossible. A missing object maps to a typed
        ``NotFoundError`` (never a leaked vendor error).
        """
        assert_key_owned_by(key, tenant_id)
        async with self._client() as client:
            try:
                response = await client.get_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in {"NoSuchKey", "404", "NotFound"}:
                    raise NotFoundError("object not found", code="object_not_found") from exc
                raise
            async with response["Body"] as stream:
                body: bytes = await stream.read()
        return body

    async def download_to_path(
        self, tenant_id: str, key: str, destination: Path
    ) -> StoredObjectMetadata:
        """Stream an object to a worker-owned bounded temporary path.

        Reads and writes in 1 MiB chunks; no full media object is materialized
        in process memory. File I/O is dispatched by AnyIO rather than blocking
        the async worker loop.
        """
        assert_key_owned_by(key, tenant_id)
        try:
            async with self._client() as client:
                response = await client.get_object(Bucket=self._bucket, Key=key)
                async with response["Body"] as stream, await open_file(destination, "wb") as output:
                    while chunk := await stream.read(1024 * 1024):
                        await output.write(chunk)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise NotFoundError("object not found", code="object_not_found") from exc
            raise DependencyError(
                "Object storage is unavailable.", code="storage_unavailable"
            ) from exc
        except BotoCoreError as exc:
            raise DependencyError(
                "Object storage is unavailable.", code="storage_unavailable"
            ) from exc
        return StoredObjectMetadata(
            key=key,
            size_bytes=int(response.get("ContentLength", 0)),
            content_type=str(response.get("ContentType", "application/octet-stream")),
            metadata={str(k): str(v) for k, v in response.get("Metadata", {}).items()},
        )

    async def delete(self, tenant_id: str, key: str) -> None:
        """Remove the object at ``key`` (AC-4).

        Tenant-prefix checked like :meth:`get`. Idempotent: deleting an absent
        key is a no-op on S3/MinIO.
        """
        assert_key_owned_by(key, tenant_id)
        try:
            async with self._client() as client:
                await client.delete_object(Bucket=self._bucket, Key=key)
        except (ClientError, BotoCoreError) as exc:
            raise DependencyError(
                "Object storage is unavailable.", code="storage_unavailable"
            ) from exc

    # --- Presigned direct transfer (AC-3) ------------------------------------

    async def presign_put(
        self,
        tenant_id: str,
        data: bytes,
        content_type: str,
        filename: str,
    ) -> tuple[str, str]:
        """Return ``(key, url)`` for a short-TTL presigned ``PUT`` upload.

        The caller validates + content-addresses up front (so the key is fixed
        and the allowlist/limit still apply), then the client transfers the bytes
        **directly** to storage — the large payload never passes through the API
        process (AC-3). ``data`` is used only to compute the content address and
        run validation; it is not uploaded here.
        """
        validate_upload(
            size_bytes=len(data),
            content_type=content_type,
            allowed_content_types=self._settings.upload_allowed_content_types,
            max_bytes=self._settings.max_upload_bytes,
        )
        key = build_key(tenant_id, data, filename)
        async with self._presign_client() as client:
            url: str = await client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": key,
                    "ContentType": content_type,
                },
                ExpiresIn=self._settings.s3_presign_ttl_seconds,
            )
        return key, url

    async def presign_get(
        self,
        tenant_id: str,
        key: str,
        *,
        download_filename: str | None = None,
        content_type: str | None = None,
    ) -> str:
        """Return a short-TTL presigned ``GET`` URL for ``key`` (AC-3).

        Tenant-prefix checked first (AC-5): a cross-prefix key is refused with
        ``ForbiddenError`` before any URL is minted, so a presigned URL can never
        be issued for another tenant's object.
        """
        assert_key_owned_by(key, tenant_id)
        try:
            async with self._presign_client() as client:
                params: dict[str, object] = {"Bucket": self._bucket, "Key": key}
                if download_filename is not None:
                    safe = download_filename.replace('"', "")
                    params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
                if content_type is not None:
                    params["ResponseContentType"] = content_type
                url: str = await client.generate_presigned_url(
                    "get_object",
                    Params=params,
                    ExpiresIn=self._settings.s3_presign_ttl_seconds,
                )
        except (ClientError, BotoCoreError) as exc:
            raise DependencyError(
                "Object storage is unavailable.", code="storage_unavailable"
            ) from exc
        return url

    # --- Direct multipart data plane (spec 0008 / ADR-0023) -----------------

    async def create_multipart_upload(
        self,
        *,
        tenant_id: str,
        document_id: str,
        upload_id: str,
        filename: str,
        content_type: str,
    ) -> MultipartUpload:
        key = build_quarantine_key(tenant_id, document_id, filename)
        try:
            async with self._client() as client:
                response = await client.create_multipart_upload(
                    Bucket=self._bucket,
                    Key=key,
                    ContentType=content_type,
                    Metadata={
                        "lumen-upload-id": upload_id,
                        "lumen-document-id": document_id,
                    },
                )
        except (ClientError, BotoCoreError) as exc:
            raise DependencyError(
                "Object storage is unavailable.", code="storage_unavailable"
            ) from exc
        return MultipartUpload(key=key, provider_upload_id=str(response["UploadId"]))

    async def presign_upload_part(
        self,
        *,
        tenant_id: str,
        key: str,
        provider_upload_id: str,
        part_number: int,
    ) -> str:
        assert_key_owned_by(key, tenant_id)
        try:
            async with self._presign_client() as client:
                url: str = await client.generate_presigned_url(
                    "upload_part",
                    Params={
                        "Bucket": self._bucket,
                        "Key": key,
                        "UploadId": provider_upload_id,
                        "PartNumber": part_number,
                    },
                    ExpiresIn=self._settings.s3_presign_ttl_seconds,
                )
        except (ClientError, BotoCoreError) as exc:
            raise DependencyError(
                "Object storage is unavailable.", code="storage_unavailable"
            ) from exc
        return url

    async def list_multipart_parts(
        self,
        *,
        tenant_id: str,
        key: str,
        provider_upload_id: str,
    ) -> list[UploadedPart]:
        assert_key_owned_by(key, tenant_id)
        parts: list[UploadedPart] = []
        marker = 0
        try:
            async with self._client() as client:
                while True:
                    response = await client.list_parts(
                        Bucket=self._bucket,
                        Key=key,
                        UploadId=provider_upload_id,
                        PartNumberMarker=marker,
                        MaxParts=1000,
                    )
                    parts.extend(
                        UploadedPart(
                            part_number=int(part["PartNumber"]),
                            etag=str(part["ETag"]),
                            size_bytes=int(part["Size"]),
                        )
                        for part in response.get("Parts", [])
                    )
                    if not response.get("IsTruncated"):
                        break
                    marker = int(response["NextPartNumberMarker"])
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"NoSuchUpload", "404", "NotFound"}:
                raise NotFoundError(
                    "multipart upload not found", code="multipart_upload_not_found"
                ) from exc
            raise DependencyError(
                "Object storage is unavailable.", code="storage_unavailable"
            ) from exc
        except BotoCoreError as exc:
            raise DependencyError(
                "Object storage is unavailable.", code="storage_unavailable"
            ) from exc
        return parts

    async def complete_multipart_upload(
        self,
        *,
        tenant_id: str,
        key: str,
        provider_upload_id: str,
        parts: list[tuple[int, str]],
    ) -> StoredObjectMetadata:
        assert_key_owned_by(key, tenant_id)
        try:
            async with self._client() as client:
                await client.complete_multipart_upload(
                    Bucket=self._bucket,
                    Key=key,
                    UploadId=provider_upload_id,
                    MultipartUpload={
                        "Parts": [
                            {"PartNumber": part_number, "ETag": etag} for part_number, etag in parts
                        ]
                    },
                )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"NoSuchUpload", "404", "NotFound"}:
                raise NotFoundError(
                    "multipart upload not found", code="multipart_upload_not_found"
                ) from exc
            raise DependencyError(
                "Object storage is unavailable.", code="storage_unavailable"
            ) from exc
        except BotoCoreError as exc:
            raise DependencyError(
                "Object storage is unavailable.", code="storage_unavailable"
            ) from exc
        return await self.head(tenant_id, key)

    async def abort_multipart_upload(
        self,
        *,
        tenant_id: str,
        key: str,
        provider_upload_id: str,
    ) -> None:
        assert_key_owned_by(key, tenant_id)
        async with self._client() as client:
            try:
                await client.abort_multipart_upload(
                    Bucket=self._bucket,
                    Key=key,
                    UploadId=provider_upload_id,
                )
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in {"NoSuchUpload", "404", "NotFound"}:
                    raise DependencyError(
                        "Object storage is unavailable.", code="storage_unavailable"
                    ) from exc
            except BotoCoreError as exc:
                raise DependencyError(
                    "Object storage is unavailable.", code="storage_unavailable"
                ) from exc

    async def head(self, tenant_id: str, key: str) -> StoredObjectMetadata:
        assert_key_owned_by(key, tenant_id)
        async with self._client() as client:
            try:
                response = await client.head_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in {"NoSuchKey", "404", "NotFound"}:
                    raise NotFoundError("object not found", code="object_not_found") from exc
                raise DependencyError(
                    "Object storage is unavailable.", code="storage_unavailable"
                ) from exc
            except BotoCoreError as exc:
                raise DependencyError(
                    "Object storage is unavailable.", code="storage_unavailable"
                ) from exc
        return StoredObjectMetadata(
            key=key,
            size_bytes=int(response["ContentLength"]),
            content_type=str(response.get("ContentType", "application/octet-stream")),
            metadata={str(k): str(v) for k, v in response.get("Metadata", {}).items()},
        )

    # --- Per-tenant application logo (admin branding) ------------------------
    #
    # A logo shares the standard upload key shape ({tenant_id}/{sha}/{name}) so
    # the generic ``presign_get`` / ``assert_key_owned_by`` seam applies unchanged
    # — only the allowlist/limit differ (image-only, small cap). No dedicated
    # namespace prefix is needed: the object is tenant-scoped and content-addressed
    # like any upload; the tenant's *current* logo is the key persisted on the
    # ``tenants`` row (a superseded logo just becomes unreferenced, like a re-upload).

    async def put_logo(
        self,
        tenant_id: str,
        data: bytes,
        content_type: str,
        filename: str,
    ) -> StoredObject:
        """Validate, content-address, and store a tenant logo; return its descriptor.

        The declared ``content_type`` and byte length are validated against the
        **logo** allowlist/limit (``LOGO_ALLOWED_CONTENT_TYPES`` / ``MAX_LOGO_BYTES``)
        **before** any write; a rejection is a typed ``ValidationError`` (→ 4xx). The
        key is ``{tenant_id}/{sha256(data)}/{safe_filename}`` (the standard upload
        shape), so retrieval reuses the generic :meth:`presign_get` and identical
        bytes dedupe. Only the allowlist/limit differ from :meth:`put`.
        """
        validate_upload(
            size_bytes=len(data),
            content_type=content_type,
            allowed_content_types=self._settings.logo_allowed_content_types,
            max_bytes=self._settings.max_logo_bytes,
        )
        key = build_key(tenant_id, data, filename)
        async with self._client() as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        return StoredObject(
            key=key,
            sha256=sha256_hex(data),
            size_bytes=len(data),
            content_type=content_type,
        )

    # --- Per-user profile avatar --------------------------------------------
    #
    # Mirrors :meth:`put_logo` but PER-USER (not admin branding): the key carries a
    # ``user_id`` segment after the tenant prefix ({tenant_id}/{user_id}/{sha}/{name})
    # so an avatar is scoped to the owning user within the tenant. The tenant prefix
    # is still the isolation seam the generic ``presign_get`` / ``assert_key_owned_by``
    # enforce, so retrieval and cross-tenant refusal are unchanged. The image-only
    # allowlist/limit are REUSED from the logo config (``logo_allowed_content_types`` /
    # ``max_logo_bytes``) — an avatar has the same "small inline image" constraints, so
    # a second knob would only add config surface without changing behaviour.

    async def put_avatar(
        self,
        tenant_id: str,
        user_id: str,
        data: bytes,
        content_type: str,
        filename: str,
    ) -> StoredObject:
        """Validate, content-address, and store a per-user avatar; return its descriptor.

        The declared ``content_type`` and byte length are validated against the
        **logo** allowlist/limit (reused for avatars — image-only, small cap)
        **before** any write; a rejection is a typed ``ValidationError`` (→ 413/415
        after the service's remap). The key is
        ``{tenant_id}/{user_id}/{sha256(data)}/{safe_filename}`` so retrieval reuses
        the generic :meth:`presign_get` and identical bytes dedupe per user.
        """
        validate_upload(
            size_bytes=len(data),
            content_type=content_type,
            allowed_content_types=self._settings.logo_allowed_content_types,
            max_bytes=self._settings.max_logo_bytes,
        )
        key = build_avatar_key(tenant_id, user_id, data, filename)
        async with self._client() as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        return StoredObject(
            key=key,
            sha256=sha256_hex(data),
            size_bytes=len(data),
            content_type=content_type,
        )

    # --- Agent/run-produced artifacts (issue #208) ---------------------------
    #
    # A parallel object lifecycle for files agents/runs *produce* (CC-12),
    # distinct from uploads: the key is namespaced under ``artifacts/`` and the
    # allowlist/limit come from the **artifact** config (broader types, its own
    # cap), while the tenant-prefix seam is enforced against
    # ``artifacts/{tenant_id}/`` (:func:`assert_artifact_key_owned_by`). The
    # ownership/visibility layer (owner-or-grant, cross-tenant → 404) lives in
    # ``services.artifacts_service``; this adapter only guarantees the tenant
    # prefix and the allowlist/limit, like the upload path.

    async def put_artifact(
        self,
        tenant_id: str,
        data: bytes,
        content_type: str,
        filename: str,
    ) -> StoredObject:
        """Validate, content-address, and store an artifact; return its descriptor.

        The declared ``content_type`` and byte length are validated against the
        **artifact** allowlist/limit (``ARTIFACT_ALLOWED_CONTENT_TYPES`` /
        ``MAX_ARTIFACT_BYTES``) **before** any write (#208 AC-2); a rejection is a
        typed ``ValidationError`` (→ 422). The key is
        ``artifacts/{tenant_id}/{sha256(data)}/{safe_filename}`` (AC-5), so
        identical agent output dedupes and every object is tenant-scoped.
        """
        validate_upload(
            size_bytes=len(data),
            content_type=content_type,
            allowed_content_types=self._settings.artifact_allowed_content_types,
            max_bytes=self._settings.max_artifact_bytes,
        )
        key = build_artifact_key(tenant_id, data, filename)
        async with self._client() as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        return StoredObject(
            key=key,
            sha256=sha256_hex(data),
            size_bytes=len(data),
            content_type=content_type,
        )

    async def get_artifact(self, tenant_id: str, key: str) -> bytes:
        """Read back the bytes of an artifact at ``key`` for ``tenant_id`` (#208).

        Enforces the artifact tenant-prefix seam first (AC-5): a key outside the
        caller's ``artifacts/{tenant_id}/`` prefix is refused with
        ``ForbiddenError`` before any I/O. A missing object maps to a typed
        ``NotFoundError`` (never a leaked vendor error).
        """
        assert_artifact_key_owned_by(key, tenant_id)
        async with self._client() as client:
            try:
                response = await client.get_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in {"NoSuchKey", "404", "NotFound"}:
                    raise NotFoundError("object not found", code="object_not_found") from exc
                raise
            async with response["Body"] as stream:
                body: bytes = await stream.read()
        return body

    async def delete_artifact(self, tenant_id: str, key: str) -> None:
        """Remove the artifact object at ``key`` (#208).

        Artifact tenant-prefix checked like :meth:`get_artifact`. Idempotent:
        deleting an absent key is a no-op on S3/MinIO.
        """
        assert_artifact_key_owned_by(key, tenant_id)
        async with self._client() as client:
            await client.delete_object(Bucket=self._bucket, Key=key)

    async def presign_get_artifact(self, tenant_id: str, key: str) -> str:
        """Return a short-TTL presigned ``GET`` URL for an artifact ``key`` (#208 AC-1).

        Artifact tenant-prefix checked first (AC-5): a cross-prefix key is refused
        with ``ForbiddenError`` before any URL is minted, so a presigned URL can
        never be issued for another tenant's artifact.
        """
        assert_artifact_key_owned_by(key, tenant_id)
        async with self._client() as client:
            url: str = await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=self._settings.s3_presign_ttl_seconds,
            )
        return url
