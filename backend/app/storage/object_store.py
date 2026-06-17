"""S3 / MinIO adapter.

The **only** constructor of an S3 client (ADR-0004). Uses ``aioboto3`` so all
object-store I/O is async and never blocks the event loop. A fresh client is
created per operation via the session's async context manager (the documented
``aioboto3`` pattern); the session itself is cheap and reused.

Exposes:
* :meth:`ensure_bucket` — create ``S3_BUCKET`` if missing; called at startup so
  the bucket exists from day one.
* :meth:`ping` — a cheap reachability check used by the readiness probe.
"""

from __future__ import annotations

from typing import Any

import aioboto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.core.config import Settings


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

    def _client(self) -> Any:
        """Return an async client context manager for the object store."""
        return self._session.client(
            "s3",
            endpoint_url=self._settings.s3_endpoint_url,
            aws_access_key_id=self._settings.s3_access_key,
            aws_secret_access_key=self._settings.s3_secret_key,
            config=self._client_config,
        )

    async def ensure_bucket(self) -> None:
        """Create the configured bucket if it does not already exist.

        Idempotent: a pre-existing bucket is a no-op. Called from the app
        lifespan so uploads have a target from first boot.
        """
        async with self._client() as client:
            try:
                await client.head_bucket(Bucket=self._bucket)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in {"404", "NoSuchBucket", "NotFound"}:
                    await client.create_bucket(Bucket=self._bucket)
                else:
                    raise

    async def ping(self) -> None:
        """Lightweight reachability check for readiness (lists buckets)."""
        async with self._client() as client:
            await client.list_buckets()
