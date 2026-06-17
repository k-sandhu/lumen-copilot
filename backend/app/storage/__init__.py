"""Object storage adapter (S3 / MinIO) — the only S3 client owner.

Single responsibility (ADR-0004 boundary table): own all object-store access
for uploads and derived artifacts. **Nobody else may construct an S3/MinIO
client.** Locally this points at MinIO; production swaps the endpoint with no
application change. Retention/sandbox rules are CC-12; this skeleton only
provides the adapter and ``ensure_bucket()`` used at startup.
"""

from app.storage.object_store import ObjectStore

__all__ = ["ObjectStore"]
