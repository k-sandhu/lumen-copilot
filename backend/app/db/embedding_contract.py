"""Readiness guard for the fixed-width embedding persistence contract (#346).

SQL and ORM inspection stay inside ``app.db`` (ADR-0004).  The API readiness
route only composes :func:`check_embedding_schema`; it never knows catalog SQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import text

from app.core.config import (
    CANONICAL_EMBEDDING_DIMENSIONS,
    LEGACY_EMBEDDING_DIMENSIONS,
    Settings,
)
from app.core.errors import DependencyError
from app.core.logging import get_logger
from app.db.session import session_scope

log = get_logger(__name__)
_VECTOR_WIDTH = re.compile(r"^vector\((\d+)\)$")


@dataclass(frozen=True, slots=True)
class EmbeddingSchemaState:
    """Content-safe dimensions observed at each persistence boundary."""

    configured_dimensions: int
    orm_dimensions: int
    postgres_dimensions: int | None
    legacy_dimensions: int | None
    pgvector_hnsw_present: bool
    parked_dimensions: int | None = None


def validate_embedding_schema(state: EmbeddingSchemaState) -> None:
    """Reject any config/ORM/catalog drift before the worker accepts traffic."""

    valid = (
        state.configured_dimensions == CANONICAL_EMBEDDING_DIMENSIONS
        and state.orm_dimensions == CANONICAL_EMBEDDING_DIMENSIONS
        and state.postgres_dimensions == CANONICAL_EMBEDDING_DIMENSIONS
        and state.legacy_dimensions == LEGACY_EMBEDDING_DIMENSIONS
        and not state.pgvector_hnsw_present
        and state.parked_dimensions is None
    )
    if valid:
        log.info(
            "embedding.contract_ready",
            configured_dimensions=state.configured_dimensions,
            orm_dimensions=state.orm_dimensions,
            postgres_dimensions=state.postgres_dimensions,
            legacy_dimensions=state.legacy_dimensions,
        )
        return

    log.error(
        "embedding.contract_mismatch",
        configured_dimensions=state.configured_dimensions,
        orm_dimensions=state.orm_dimensions,
        postgres_dimensions=state.postgres_dimensions,
        legacy_dimensions=state.legacy_dimensions,
        pgvector_hnsw_present=state.pgvector_hnsw_present,
        parked_dimensions=state.parked_dimensions,
    )
    raise DependencyError(
        "Embedding config and storage schema do not share the canonical 2,048-dimension contract.",
        code="embedding_dimension_mismatch",
    )


def _dimension(type_name: str | None) -> int | None:
    if type_name is None:
        return None
    match = _VECTOR_WIDTH.fullmatch(type_name)
    return int(match.group(1)) if match else None


async def check_embedding_schema(settings: Settings) -> EmbeddingSchemaState:
    """Inspect pgvector catalog state and validate it against config + ORM."""

    # Import lazily so migration tooling can import config without registering
    # every ORM row twice during Alembic environment setup.
    from app.db.models import Chunk

    orm_dimensions = getattr(Chunk.__table__.c.embedding.type, "dim", None)
    if not isinstance(orm_dimensions, int):
        raise DependencyError(
            "The ORM embedding column does not declare a fixed vector width.",
            code="embedding_dimension_mismatch",
        )

    async with session_scope() as session:
        result = await session.execute(
            text(
                """
SELECT
    (SELECT format_type(a.atttypid, a.atttypmod)
       FROM pg_attribute a
      WHERE a.attrelid = 'chunks'::regclass
        AND a.attname = 'embedding'
        AND NOT a.attisdropped) AS active_type,
    (SELECT format_type(a.atttypid, a.atttypmod)
       FROM pg_attribute a
      WHERE a.attrelid = 'chunks'::regclass
        AND a.attname = 'embedding_legacy_1024'
        AND NOT a.attisdropped) AS legacy_type,
    (SELECT format_type(a.atttypid, a.atttypmod)
       FROM pg_attribute a
      WHERE a.attrelid = 'chunks'::regclass
        AND a.attname = 'embedding_2048'
        AND NOT a.attisdropped) AS parked_type,
    to_regclass('ix_chunks_embedding_hnsw') IS NOT NULL AS old_hnsw_present
"""
            )
        )
        row = result.one()

    state = EmbeddingSchemaState(
        configured_dimensions=settings.llm_embedding_dimensions,
        orm_dimensions=orm_dimensions,
        postgres_dimensions=_dimension(row.active_type),
        legacy_dimensions=_dimension(row.legacy_type),
        pgvector_hnsw_present=bool(row.old_hnsw_present),
        parked_dimensions=_dimension(row.parked_type),
    )
    validate_embedding_schema(state)
    return state
