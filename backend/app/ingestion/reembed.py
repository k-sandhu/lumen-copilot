"""Controlled, resumable native-vector cut-over for migration 0044 (#346).

Preview is the default and performs no writes or broker publishes::

    uv run python -m app.ingestion.reembed

After the runbook preflight, enqueue one bounded page::

    uv run python -m app.ingestion.reembed --execute --limit 200

Successful ingestion fills ``embedding vector(2048)`` while preserving the
legacy 1,024 vector. A rerun selects only rows still missing a native vector, so
the command resumes after interruption without padding, truncating, or duplicate
chunks. The ordinary ingestion task remains the one enqueue/write seam.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID

import structlog

from app.core.config import get_settings
from app.core.errors import DependencyError
from app.db.repositories import EmbeddingReconcileRepository
from app.db.session import dispose_engine, session_scope
from app.db.tenant_context import bind_bypass
from app.ingestion.contract import provision_embedding_contract
from app.tasks.ingest import enqueue_ingestion

log = structlog.get_logger(__name__)


async def _preflight() -> None:
    """Prove every fixed-width boundary before publishing any work."""

    settings = get_settings()
    if not settings.llm_enabled:
        raise DependencyError(
            "OPENROUTER_API_KEY is required for controlled re-embedding.",
            code="llm_unconfigured",
        )
    await provision_embedding_contract(settings)


async def _candidates(*, limit: int, tenant_id: UUID | None) -> list[tuple[UUID, UUID]]:
    settings = get_settings()
    async with session_scope() as session:
        await bind_bypass(session)
        return await EmbeddingReconcileRepository(session).list_requiring_reembedding(
            limit=limit,
            target_fingerprint=settings.embedding_space_fingerprint,
            tenant_id=tenant_id,
        )


async def _reserve(*, limit: int, tenant_id: UUID | None) -> list[tuple[UUID, UUID]]:
    """Commit one SKIP-LOCKED reservation page before any broker I/O."""

    settings = get_settings()
    async with session_scope() as session:
        await bind_bypass(session)
        return await EmbeddingReconcileRepository(session).reserve_reembedding(
            limit=limit,
            target_fingerprint=settings.embedding_space_fingerprint,
            tenant_id=tenant_id,
        )


async def _release(*, tenant_id: UUID, document_id: UUID) -> bool:
    """Make a definitely-unpublished reservation visible to the next rerun."""

    settings = get_settings()
    async with session_scope() as session:
        await bind_bypass(session)
        return await EmbeddingReconcileRepository(session).release_reembedding(
            tenant_id=tenant_id,
            document_id=document_id,
            target_fingerprint=settings.embedding_space_fingerprint,
        )


async def _main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        if not args.execute:
            candidates = await _candidates(limit=args.limit, tenant_id=args.tenant)
            print(  # noqa: T201 — operator CLI feedback is the purpose
                f"preview: {len(candidates)} document(s) require native re-embedding; "
                "no jobs published"
            )
            return

        await _preflight()
        candidates = await _reserve(limit=args.limit, tenant_id=args.tenant)
        published = 0
        deferred = 0
        for tenant_id, document_id in candidates:
            accepted = enqueue_ingestion(tenant_id, document_id)
            outcome = "published" if accepted else "broker_failed_released"
            released = False
            if accepted:
                published += 1
            else:
                deferred += 1
                released = await _release(tenant_id=tenant_id, document_id=document_id)
            event = {
                "event": "embedding_reembed.publish",
                "tenant_id": str(tenant_id),
                "document_id": str(document_id),
                "outcome": outcome,
                "reservation_released": released,
            }
            log.info(
                "embedding_reembed.publish",
                tenant_id=str(tenant_id),
                document_id=str(document_id),
                outcome=outcome,
                reservation_released=released,
            )
            print(json.dumps(event, sort_keys=True))  # noqa: T201
        print(  # noqa: T201
            f"reserved: {len(candidates)}; published: {published}; deferred: {deferred}; "
            "rerun preview after workers drain"
        )
        if deferred:
            raise SystemExit(1)
    finally:
        await dispose_engine()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview or enqueue lossless native-2048 re-embedding (#346)."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Publish the bounded candidate page (default is a read-only preview).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        choices=range(1, 1001),
        metavar="1..1000",
        help="Maximum documents to inspect/publish this run (default: 200).",
    )
    parser.add_argument(
        "--tenant",
        type=UUID,
        default=None,
        help="Restrict the cut-over to one tenant id (default: every tenant).",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    asyncio.run(_main())
