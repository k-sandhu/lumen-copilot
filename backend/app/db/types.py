"""Portable column types for the ORM.

Production runs on PostgreSQL 16 + pgvector (ADR-0003); the ``chunks.embedding``
column is a native ``vector(N)`` so retrieval can use ANN indexes (#21). But the
repository unit tests run on in-memory SQLite (no Postgres needed to assert the
tenant-scoping invariants, INV-1), where ``vector`` does not exist.

:class:`Embedding` bridges the two: on the PostgreSQL dialect it compiles to
pgvector's ``Vector(dim)``; on any other dialect it degrades to a JSON-encoded
list of floats. The Python value on both sides is ``list[float] | None``, so the
models and repositories are dialect-agnostic and the boundary (domain
``tuple[float, ...]``) is unaffected. The real ``vector`` column shape is pinned
by the Alembic migration, which targets Postgres only.
"""

from __future__ import annotations

import json
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Dialect, types
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.types import TypeDecorator


class StringArray(TypeDecorator[list[str]]):
    """A list of short strings (e.g. RBAC roles), portable across dialects.

    PostgreSQL → native ``text[]`` (so a future predicate can use array ops);
    everything else → ``TEXT`` holding a JSON array. The Python value is
    ``list[str]`` on both sides, so the models/repositories are dialect-agnostic.
    """

    impl = types.Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> types.TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(ARRAY(types.Text()))
        return dialect.type_descriptor(types.Text())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return list(value)
        return json.dumps(list(value))

    def process_result_value(self, value: Any, dialect: Dialect) -> list[str] | None:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return list(value)
        return [str(x) for x in json.loads(value)]


class Embedding(TypeDecorator[list[float]]):
    """A fixed-dimension embedding vector, portable across dialects.

    PostgreSQL → pgvector ``Vector(dim)``; everything else → ``TEXT`` holding a
    JSON array. ``dim`` pins the width (``LLM_EMBEDDING_DIMENSIONS`` = 1024).
    """

    impl = types.Text
    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim
        super().__init__()

    def load_dialect_impl(self, dialect: Dialect) -> types.TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Vector(self.dim))
        return dialect.type_descriptor(types.Text())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            # pgvector accepts a Python list directly.
            return list(value)
        return json.dumps(list(value))

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        if value is None:
            return None
        if dialect.name == "postgresql":
            # pgvector returns a numpy array; normalize to a plain list.
            return [float(x) for x in value]
        return [float(x) for x in json.loads(value)]
