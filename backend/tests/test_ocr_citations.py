"""Machine-read provenance survives live and stored citation projection."""

from uuid import uuid4

from app.api.v1.chat import _citation_to_response
from app.db.repositories import CitationView
from app.domain.chat import GroundedCitation
from app.domain.retrieval import RetrievedPassage
from app.services.chat_runtime import _citation_event_data


def test_machine_read_label_and_redaction():
    passage = RetrievedPassage(
        uuid4(), uuid4(), "generated scan", 0, "café 😀", 0, 6, 1, machine_read=True
    )
    grounded = GroundedCitation.from_passage(passage)
    assert grounded.machine_read
    assert _citation_event_data(grounded)["machineRead"] is True
    view = CitationView(
        uuid4(),
        uuid4(),
        passage.document_id,
        passage.document_name,
        passage.chunk_id,
        passage.text,
        0,
        6,
        1,
        machine_read=True,
    )
    assert _citation_to_response(view).model_dump(exclude_none=True)["machine_read"] is True
    assert "machine_read" not in _citation_to_response(view.redact()).model_dump(exclude_none=True)
