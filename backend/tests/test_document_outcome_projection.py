"""Additive status contract, including legacy zero-chunk negatives."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.api.v1.documents import _to_response
from app.domain.entities import Document, DocumentStatus
from app.services.document_service import DocumentView


@pytest.mark.parametrize(
    ("status", "count", "outcome", "searchable"),
    [
        (DocumentStatus.READY, 0, "empty", False),
        (DocumentStatus.READY, 1, "indexed", True),
        (DocumentStatus.PROCESSING, 1, None, False),
        (DocumentStatus.FAILED, 0, "unsupported", False),
        (DocumentStatus.READY, 1, "empty", False),
        (DocumentStatus.READY, 1, "failed", False),
        (DocumentStatus.READY, 1, "unsupported", False),
    ],
)
def test_status_contract_distinguishes_native_outcome_and_searchability(
    status: DocumentStatus, count: int, outcome: str | None, searchable: bool
) -> None:
    now = datetime.now(UTC)
    document = Document(
        id=uuid4(),
        tenant_id=uuid4(),
        owner_id=uuid4(),
        collection_id=uuid4(),
        filename="synthetic.pdf",
        mime_type="application/pdf",
        size_bytes=0,
        storage_key="synthetic",
        status=status,
        error=None,
        created_at=now,
        updated_at=now,
        ingestion_metadata={"ingestion_outcome": outcome},
    )
    payload = _to_response(DocumentView(document, count)).model_dump()
    assert "ingestion_outcome" in payload
    assert payload["ingestion_outcome"] == outcome
    assert payload["searchable"] is searchable
