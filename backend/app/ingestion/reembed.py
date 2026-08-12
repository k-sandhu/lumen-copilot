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
from uuid import UUID

from app.core.config import get_settings
from app.core.errors import DependencyError
from app.db.embedding_contract import check_embedding_schema
from app.db.repositories import EmbeddingReconcileRepository
from app.db.session import dispose_engine, session_scope
from app.db.tenant_context import bind_bypass
from app.llm import LLMGateway
from app.search import OpenSearchStore
from app.tasks.ingest import enqueue_ingestion


async def _preflight() -> None:
    """Prove every fixed-width boundary before publishing any work."""

    settings = get_settings()
    await check_embedding_schema(settings)

    store = OpenSearchStore.from_settings(settings)
    try:
        await store.ensure_index()
        await store.check_embedding_dimensions()
    finally:
        await store.aclose()

    if not settings.llm_enabled:
        raise DependencyError(
            "OPENROUTER_API_KEY is required for controlled re-embedding.",
            code="llm_unconfigured",
        )
    await LLMGateway(settings).embed(
        ["lumen embedding migration preflight"],
        cache_namespace="embedding-migration-preflight",
    )


async def _candidates(*, limit: int, tenant_id: UUID | None) -> list[tuple[UUID, UUID]]:
    async with session_scope() as session:
        await bind_bypass(session)
        return await EmbeddingReconcileRepository(session).list_requiring_reembedding(
            limit=limit,
            tenant_id=tenant_id,
        )


async def _main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        candidates = await _candidates(limit=args.limit, tenant_id=args.tenant)
        if not args.execute:
            print(  # noqa: T201 — operator CLI feedback is the purpose
                f"preview: {len(candidates)} document(s) require native re-embedding; "
                "no jobs published"
            )
            return

        await _preflight()
        for tenant_id, document_id in candidates:
            enqueue_ingestion(tenant_id, document_id)
        print(  # noqa: T201
            f"requested: {len(candidates)} document(s); rerun preview after workers drain"
        )
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
