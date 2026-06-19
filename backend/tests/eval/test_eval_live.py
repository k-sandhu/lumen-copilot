"""Live eval run — real OpenRouter model + real pgvector hybrid (#29).

The same golden set as the offline run, but against the **real** stack: documents
are ingested with real bge-m3 embeddings, retrieval is the real pgvector +
full-text hybrid (:class:`app.retrieval.RetrievalService`), and the answer comes
from a real model through the LiteLLM gateway. This is the proof that answer
quality holds on the genuine path — it cannot run without a key + Postgres, so it
**skips cleanly** when either is absent (mirroring ``test_retrieval_service`` and
``test_llm_gateway``).

It is intentionally one consolidated test (one seeded schema, the whole golden
set) to keep the live cost bounded, and it asserts the relaxed live thresholds
(:meth:`Thresholds.live`) — high enough to forbid systemic ungrounding/mis-citing
while tolerating a single soft miss a real model may produce on the tiny corpus.

Run it with the stack up::

    docker compose up -d
    export OPENROUTER_API_KEY=sk-...
    cd backend && uv run --extra dev pytest tests/eval/test_eval_live.py -v
"""

from __future__ import annotations

import os
import socket
import uuid
from urllib.parse import urlparse

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.principal import Principal
from app.core.config import Settings
from app.db.base import Base
from app.db.repositories import (
    ChatSessionRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import Role
from app.llm import LLMGateway
from app.retrieval import RetrievalService
from tests.eval.golden import GOLDEN_DOCUMENTS
from tests.eval.harness import run_eval, seed_corpus
from tests.eval.scoring import Thresholds, aggregate, assert_meets

import app.db.models  # noqa: F401  isort: skip

_PG_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://lumen:lumen_local_dev@localhost:47182/lumen"
)


def _pg_reachable(url: str) -> bool:
    parsed = urlparse(url.replace("postgresql+asyncpg", "postgresql"))
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


_has_key = bool(os.environ.get("OPENROUTER_API_KEY", "").strip())
_pg_up = _pg_reachable(_PG_URL)

_live = pytest.mark.skipif(
    not (_has_key and _pg_up),
    reason=(
        "live eval needs OPENROUTER_API_KEY + a reachable Postgres "
        f"(key={'set' if _has_key else 'unset'}, pg@{_PG_URL}={'up' if _pg_up else 'down'}); "
        "skipped (offline-safe)."
    ),
)


@_live
async def test_live_eval_meets_thresholds() -> None:
    """End-to-end on the real stack: ingest → hybrid retrieve → grounded answer.

    Seeds the golden corpus into an isolated schema with real embeddings, runs the
    whole golden set through the real retrieval + the real model, and asserts the
    aggregate metrics clear the live thresholds. The schema is dropped in teardown
    so the live run never pollutes app data.
    """
    settings = Settings()  # type: ignore[call-arg]  # reads real env / .env
    gateway = LLMGateway(settings)
    model = settings.llm_model

    engine = create_async_engine(_PG_URL)
    schema = f"eval_{uuid.uuid4().hex[:8]}"
    try:
        async with engine.begin() as conn:
            await conn.execute(sql_text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(sql_text(f'CREATE SCHEMA "{schema}"'))
            # Include `public` so the pgvector `vector` type (the extension lives
            # in public) resolves while new tables are created in the eval schema.
            await conn.execute(sql_text(f'SET search_path TO "{schema}", public'))
            await conn.run_sync(Base.metadata.create_all)

        # Pin the search_path to the isolated schema on every new DB connection,
        # so the runtime's own sessions (persistence) and the read sessions
        # (retrieval, seeding) all see the eval tables.
        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "connect")
        def _set_search_path(dbapi_conn: object, _record: object) -> None:
            cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
            cur.execute(f'SET search_path TO "{schema}", public')
            cur.close()

        factory = async_sessionmaker(bind=engine, expire_on_commit=False)

        async with factory() as seed:
            await seed.execute(sql_text(f'SET search_path TO "{schema}", public'))
            tenant = await TenantRepository(seed).create(name="Acme")
            user = await UserRepository(seed, tenant.id).create(
                email="kw@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            principal = Principal(user_id=user.id, tenant_id=tenant.id, roles=(Role.MEMBER,))
            # Real embeddings (the gateway) so the pgvector leg is meaningful.
            await seed_corpus(
                seed,
                tenant_id=tenant.id,
                owner_id=user.id,
                documents=GOLDEN_DOCUMENTS,
                embed=gateway.embed,
            )
            await seed.commit()

        async def _new_session_id() -> uuid.UUID:
            async with factory() as session:
                row = await ChatSessionRepository(session, tenant.id).create(
                    owner_id=user.id, model=model, title="eval"
                )
                await session.commit()
                return row.id

        async with factory() as retrieval_session:
            retrieval = RetrievalService(retrieval_session, gateway=gateway)
            scores = await run_eval(
                sessionmaker=factory,
                retrieval=retrieval,
                gateway=gateway,
                principal=principal,
                new_session_id=_new_session_id,
                model=model,
            )

        score = aggregate(scores)
        assert_meets(score, Thresholds.live())
    finally:
        async with engine.begin() as conn:
            await conn.execute(sql_text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
