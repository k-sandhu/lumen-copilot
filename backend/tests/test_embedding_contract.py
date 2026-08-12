"""Canonical embedding schema/config contract regressions (#346)."""

from __future__ import annotations

import pytest

from app.core.errors import DependencyError
from app.db.embedding_contract import EmbeddingSchemaState, validate_embedding_schema


def test_matching_embedding_schema_contract_is_accepted() -> None:
    validate_embedding_schema(
        EmbeddingSchemaState(
            configured_dimensions=2048,
            orm_dimensions=2048,
            postgres_dimensions=2048,
            legacy_dimensions=1024,
            pgvector_hnsw_present=False,
        )
    )


@pytest.mark.parametrize(
    ("postgres_dimensions", "hnsw_present"),
    [(1024, False), (2048, True)],
)
def test_schema_or_index_dimension_drift_refuses_readiness(
    postgres_dimensions: int, hnsw_present: bool
) -> None:
    state = EmbeddingSchemaState(
        configured_dimensions=2048,
        orm_dimensions=2048,
        postgres_dimensions=postgres_dimensions,
        legacy_dimensions=1024,
        pgvector_hnsw_present=hnsw_present,
    )
    with pytest.raises(DependencyError) as excinfo:
        validate_embedding_schema(state)
    assert excinfo.value.code == "embedding_dimension_mismatch"


def test_parked_native_column_refuses_readiness_even_at_the_right_width() -> None:
    """R1-007: active+parked is an ambiguous state that cannot safely roll back."""

    state = EmbeddingSchemaState(
        configured_dimensions=2048,
        orm_dimensions=2048,
        postgres_dimensions=2048,
        legacy_dimensions=1024,
        pgvector_hnsw_present=False,
        parked_dimensions=2048,
    )
    with pytest.raises(DependencyError) as excinfo:
        validate_embedding_schema(state)
    assert excinfo.value.code == "embedding_dimension_mismatch"
