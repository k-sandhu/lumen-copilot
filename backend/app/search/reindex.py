"""Search-index backfill — reindex the corpus into OpenSearch (ADR-0010 §5).

    uv run python -m app.search.reindex            # every tenant
    uv run python -m app.search.reindex --tenant <uuid>

The operator command that makes the engine match Postgres for the **whole
corpus**: enumerate tenants → keyset-page each tenant's document ids → run the
same per-document sync the write path uses
(:func:`app.tasks.index_sync.sync_document_index_async`). Because that sync is
*converge-to-source-of-truth*, the backfill is **idempotent** (a re-run
re-syncs to the same state) and **resumable** (per-document unit of work,
deterministic id order — re-running after a crash simply re-covers ground).

Used for the initial ADR-0010 cutover backfill and to repair any gap the
write path logged (``index_sync.exhausted`` / ``index_sync.enqueue_failed``).
Fails fast on an unreachable engine (that is the point of the run); exits
non-zero so operators notice. Mirrors the :mod:`app.auth.seed` CLI shape.
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from app.core.config import get_settings
from app.db.repositories import DocumentRepository, TenantRepository
from app.db.session import dispose_engine, session_scope
from app.search.store import OpenSearchStore
from app.tasks.index_sync import sync_document_index_async

# Documents enumerated per keyset page (id-ordered); each document still syncs
# individually, so the page size only bounds enumeration memory, not batch risk.
_PAGE_SIZE = 200


async def reindex_tenant(
    tenant_id: UUID,
    *,
    store: OpenSearchStore,
    page_size: int = _PAGE_SIZE,
) -> int:
    """Reindex every document of one tenant; returns the documents synced."""
    settings = get_settings()
    synced = 0
    after: UUID | None = None
    while True:
        async with session_scope() as session:
            ids = await DocumentRepository(session, tenant_id).list_ids_page(
                after_id=after, limit=page_size
            )
        if not ids:
            return synced
        for document_id in ids:
            await sync_document_index_async(
                tenant_id, document_id, settings=settings, store=store
            )
            synced += 1
        after = ids[-1]


async def _main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    settings = get_settings()
    store = OpenSearchStore.from_settings(settings)
    try:
        await store.ensure_index()
        async with session_scope() as session:
            tenants = await TenantRepository(session).list_all()
        if args.tenant is not None:
            tenants = [t for t in tenants if t.id == args.tenant]
            if not tenants:
                raise SystemExit(f"no tenant with id {args.tenant}")
        total = 0
        for tenant in tenants:
            count = await reindex_tenant(tenant.id, store=store)
            total += count
            print(  # noqa: T201 — CLI feedback is the point
                f"tenant {tenant.id} ({tenant.name}): {count} document(s) reindexed"
            )
        print(f"done: {total} document(s) across {len(tenants)} tenant(s)")  # noqa: T201
    finally:
        await store.aclose()
        await dispose_engine()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reindex the document corpus into OpenSearch (ADR-0010 §5)."
    )
    parser.add_argument(
        "--tenant",
        type=UUID,
        default=None,
        help="Restrict the backfill to one tenant id (default: every tenant).",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    asyncio.run(_main())
