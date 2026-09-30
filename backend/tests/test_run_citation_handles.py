"""Run-detail serialization preserves the shared citation handle contract."""

from uuid import uuid4

import pytest

from app.api.v1.runs import _citation_to_response
from app.db.repositories import CitationView


@pytest.mark.parametrize("handle", [None, "S1001"])
def test_run_detail_preserves_citation_handle(handle: str | None) -> None:
    citation = CitationView(
        id=uuid4(),
        message_id=uuid4(),
        document_id=uuid4(),
        document_name="Source",
        chunk_id=uuid4(),
        snippet="Permitted fact",
        char_start=0,
        char_end=14,
        score=None,
        handle=handle,
    )

    assert _citation_to_response(citation).handle == handle
    redacted = _citation_to_response(citation.redact())
    assert redacted.handle == handle
    assert redacted.redacted and redacted.snippet == "" and redacted.document_name == ""
