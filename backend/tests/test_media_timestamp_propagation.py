"""Timestamp provenance stays attached to retrieval and grounded citations (#571)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx

from app.api.v1.chat import _citation_to_response as chat_citation_to_response
from app.api.v1.runs import _citation_to_response as run_citation_to_response
from app.api.v1.search import _to_citation as search_citation_to_response
from app.api.v1.search import _to_result as search_result_to_response
from app.domain.chat import GroundedCitation
from app.domain.entities import DocumentKind
from app.domain.retrieval import RetrievedPassage
from app.retrieval.queries import PassageRow, _base_chunk_select, _valid_passage_provenance
from app.search import IndexedChunk, OpenSearchStore
from app.search.store import _index_body
from app.services.chat_runtime import _citation_event_data
from app.services.search_service import MatchSpanData, SearchCitationData, SearchResultData
from app.services.tools.impls.retrieval import _render_passages
from app.tasks.index_sync import _to_indexed


def _media_passage() -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_name="meeting.mp4",
        ord=4,
        text="Hello, my name is John.",
        char_start=120,
        char_end=143,
        score=0.91,
        time_start_ms=61_250,
        time_end_ms=64_900,
        transcript_segment_id=uuid.uuid4(),
        speaker_id="speaker-2",
        speaker_name="John",
    )


def test_grounded_media_citation_preserves_player_provenance() -> None:
    passage = _media_passage()

    citation = GroundedCitation.from_passage(passage)

    assert citation.time_start_ms == 61_250
    assert citation.time_end_ms == 64_900
    assert citation.transcript_segment_id == passage.transcript_segment_id
    assert citation.speaker_id == "speaker-2"
    assert citation.speaker_name == "John"


def test_model_visible_media_evidence_names_speaker_and_time_range() -> None:
    rendered = _render_passages([_media_passage()])

    assert "01:01.250-01:04.900" in rendered
    assert "John (speaker-2)" in rendered


def test_websocket_citation_payload_carries_media_provenance_and_omits_text_nulls() -> None:
    media = GroundedCitation.from_passage(_media_passage())
    media_payload = _citation_event_data(media)

    assert media_payload["timeStartMs"] == 61_250
    assert media_payload["timeEndMs"] == 64_900
    assert media_payload["transcriptSegmentId"] == str(media.transcript_segment_id)
    assert media_payload["speakerId"] == "speaker-2"
    assert media_payload["speakerName"] == "John"

    text = GroundedCitation(
        id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_name="brief.pdf",
        chunk_id=uuid.uuid4(),
        snippet="ordinary text",
        char_start=0,
        char_end=13,
    )
    text_payload = _citation_event_data(text)
    assert "timeStartMs" not in text_payload
    assert "speakerId" not in text_payload


def test_rest_citation_projections_redact_media_provenance_defensively() -> None:
    segment_id = uuid.uuid4()
    base = {
        "id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "document_name": "meeting.mp4",
        "chunk_id": uuid.uuid4(),
        "snippet": "Hello, my name is John.",
        "char_start": 120,
        "char_end": 143,
        "score": 0.91,
        "time_start_ms": 61_250,
        "time_end_ms": 64_900,
        "transcript_segment_id": segment_id,
        "speaker_id": "speaker-2",
        "speaker_name": "John",
    }

    for project in (chat_citation_to_response, run_citation_to_response):
        visible = project(SimpleNamespace(**base, redacted=False)).model_dump(exclude_none=True)
        assert visible["time_start_ms"] == 61_250
        assert visible["transcript_segment_id"] == segment_id

        redacted = project(SimpleNamespace(**base, redacted=True)).model_dump(exclude_none=True)
        assert "time_start_ms" not in redacted
        assert "transcript_segment_id" not in redacted
        assert "speaker_name" not in redacted


def test_search_wire_projections_carry_media_provenance() -> None:
    segment_id = uuid.uuid4()
    result = SearchResultData(
        id=uuid.uuid4(),
        title="meeting.mp4",
        snippet="Hello, my name is John.",
        match_spans=[MatchSpanData(start=0, end=5)],
        why_matched="semantic",
        source="upload",
        type="document",
        permission="allowed",
        last_indexed=datetime.now(UTC),
        document_id=uuid.uuid4(),
        document_kind=DocumentKind.VIDEO.value,
        time_start_ms=61_250,
        time_end_ms=64_900,
        transcript_segment_id=segment_id,
        speaker_id="speaker-2",
        speaker_name="John",
    )
    result_wire = search_result_to_response(result).model_dump(exclude_none=True)
    assert result_wire["document_kind"] == "video"
    assert result_wire["time_start_ms"] == 61_250
    assert result_wire["speaker_name"] == "John"

    citation = SearchCitationData(
        result_id=result.id,
        snippet=result.snippet,
        char_start=120,
        char_end=143,
        time_start_ms=result.time_start_ms,
        time_end_ms=result.time_end_ms,
        transcript_segment_id=segment_id,
        speaker_id=result.speaker_id,
        speaker_name=result.speaker_name,
    )
    citation_wire = search_citation_to_response(citation).model_dump(exclude_none=True)
    assert citation_wire["time_end_ms"] == 64_900
    assert citation_wire["transcript_segment_id"] == segment_id


def test_index_sync_projection_carries_media_fields() -> None:
    segment_id = uuid.uuid4()
    document = SimpleNamespace(
        owner_id=uuid.uuid4(),
        collection_id=uuid.uuid4(),
        acl_enforced=False,
        acl_principals=(),
        acl_synced_at=None,
        acl_scope_ids=(),
    )
    chunk = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        ord=0,
        text="Hello, my name is John.",
        embedding=None,
        char_start=0,
        char_end=23,
        time_start_ms=1_000,
        time_end_ms=3_000,
        transcript_segment_id=segment_id,
        speaker_id="speaker-1",
        speaker_name="John",
    )

    indexed = _to_indexed(document, [chunk])[0]

    assert indexed.time_start_ms == 1_000
    assert indexed.time_end_ms == 3_000
    assert indexed.transcript_segment_id == segment_id
    assert indexed.speaker_name == "John"


def test_relational_hydration_selects_authoritative_media_provenance() -> None:
    selected = set(_base_chunk_select().selected_columns.keys())

    assert {
        "time_start_ms",
        "time_end_ms",
        "transcript_segment_id",
        "speaker_id",
        "speaker_name",
    } <= selected


def test_retrieval_blocks_invalid_or_out_of_duration_media_provenance() -> None:
    common = {
        "chunk_id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "document_name": "meeting.mp4",
        "document_kind": "video",
        "duration_ms": 5_000,
        "ord": 0,
        "text": "hello",
        "char_start": 0,
        "char_end": 5,
        "transcript_segment_id": uuid.uuid4(),
        "transcript_segment_document_id": None,
        "speaker_id": "speaker-1",
        "speaker_name": None,
    }

    common["transcript_segment_document_id"] = common["document_id"]
    assert _valid_passage_provenance(PassageRow(**common, time_start_ms=1_000, time_end_ms=3_000))
    assert not _valid_passage_provenance(
        PassageRow(
            **{**common, "transcript_segment_document_id": uuid.uuid4()},
            time_start_ms=1_000,
            time_end_ms=3_000,
        )
    )
    # A retrieval chunk may span multiple transcript turns. Its paired timeline
    # remains authoritative, but there is deliberately no single segment or
    # speaker to attach to the whole passage.
    multi_turn = {
        **common,
        "transcript_segment_id": None,
        "transcript_segment_document_id": None,
        "speaker_id": None,
        "speaker_name": None,
    }
    assert _valid_passage_provenance(
        PassageRow(**multi_turn, time_start_ms=1_000, time_end_ms=3_000)
    )
    assert not _valid_passage_provenance(
        PassageRow(**common, time_start_ms=1_000, time_end_ms=6_000)
    )
    assert not _valid_passage_provenance(PassageRow(**common, time_start_ms=None, time_end_ms=None))

    ordinary = PassageRow(
        **{**common, "document_kind": "document", "duration_ms": None},
        time_start_ms=1_000,
        time_end_ms=3_000,
    )
    assert not _valid_passage_provenance(ordinary)


def test_relational_schema_fences_segment_provenance_to_its_source_document() -> None:
    from sqlalchemy import ForeignKeyConstraint

    from app.db import models

    chunk_fks = {
        constraint.name: (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in models.Chunk.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    citation_fks = {
        constraint.name: (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in models.Citation.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }

    assert chunk_fks["fk_chunks_transcript_segment_document"] == (
        ("transcript_segment_id", "document_id"),
        ("transcript_segments.id", "transcript_segments.document_id"),
    )
    assert citation_fks["fk_citations_chunk_transcript_segment"] == (
        ("chunk_id", "transcript_segment_id"),
        ("chunks.id", "chunks.transcript_segment_id"),
    )


def test_opensearch_strict_mapping_declares_media_fields() -> None:
    properties = _index_body(8)["mappings"]["properties"]

    assert properties["time_start_ms"] == {"type": "long"}
    assert properties["time_end_ms"] == {"type": "long"}
    assert properties["transcript_segment_id"] == {"type": "keyword"}
    assert properties["speaker_id"] == {"type": "keyword"}
    assert properties["speaker_name"] == {"type": "keyword"}


async def test_index_payload_carries_media_fields_and_omits_nulls() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.extend(json.loads(line) for line in request.content.decode().strip().splitlines())
        return httpx.Response(200, json={"errors": False, "items": []})

    store = OpenSearchStore(
        base_url="http://opensearch.test:9200",
        index="media-test",
        dimensions=8,
        client=httpx.AsyncClient(
            base_url="http://opensearch.test:9200", transport=httpx.MockTransport(handler)
        ),
    )
    common = {
        "tenant_id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "owner_id": uuid.uuid4(),
        "collection_id": uuid.uuid4(),
        "ord": 0,
        "text": "hello",
        "embedding": None,
        "char_start": 0,
        "char_end": 5,
    }
    media = IndexedChunk(
        chunk_id=uuid.uuid4(),
        **common,
        time_start_ms=1_000,
        time_end_ms=2_000,
        transcript_segment_id=uuid.uuid4(),
        speaker_id="speaker-1",
        speaker_name="John",
    )
    text = IndexedChunk(chunk_id=uuid.uuid4(), **common)

    await store.upsert_chunks([media, text])
    await store.aclose()

    media_doc, text_doc = captured[1], captured[3]
    assert media_doc["time_start_ms"] == 1_000
    assert media_doc["time_end_ms"] == 2_000
    assert media_doc["speaker_name"] == "John"
    assert "time_start_ms" not in text_doc
    assert "speaker_id" not in text_doc
