from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator, Sequence
from importlib import import_module

import pytest
import pytest_asyncio

_PDF = "application/pdf"
_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _make_pdf_pages_with_blank_middle() -> bytes:
    from pypdf import PdfReader, PdfWriter

    from tests.test_ingestion_parsers import _make_pdf

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(_make_pdf("First page sentinel"))))
    writer.add_blank_page(width=612, height=792)
    writer.append(PdfReader(io.BytesIO(_make_pdf("Final page sentinel"))))
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _make_presentation_with_blank_second_slide() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    first = presentation.slides.add_slide(presentation.slide_layouts[0])
    first.shapes.title.text = "Project plan"
    body = first.shapes.add_textbox(Inches(1), Inches(2), Inches(5), Inches(1))
    body.text_frame.text = "Milestone detail"
    presentation.slides.add_slide(presentation.slide_layouts[6])
    output = io.BytesIO()
    presentation.save(output)
    return output.getvalue()


def _make_workbook_with_blank_middle_sheet() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    north = workbook.active
    north.title = "North"
    north.append(["Region", "Revenue"])
    north.append(["North", 12])
    workbook.create_sheet("Blank Middle")
    south = workbook.create_sheet("South")
    south.append(["Region", "Revenue"])
    south.append(["South", 18])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _assert_location_bounds(parsed: object) -> None:
    text = parsed.text
    for location in parsed.locations:
        assert 0 <= location.char_start <= location.char_end <= len(text)


@pytest.mark.parametrize(
    ("mime_type", "data"),
    [(_PDF, b"not a PDF"), (_PPTX, b"not a PPTX"), (_XLSX, b"not an XLSX")],
)
def test_corrupt_native_formats_keep_typed_parse_errors(mime_type: str, data: bytes) -> None:
    from app.ingestion.parsers import DocumentParseError, parse_document_with_locations

    with pytest.raises(DocumentParseError):
        parse_document_with_locations(data, mime_type=mime_type)


def test_pdf_locations_cover_named_pages_and_preserve_blank_page_span() -> None:
    from app.ingestion.parsers import parse_document, parse_document_with_locations

    data = _make_pdf_pages_with_blank_middle()
    parsed = parse_document_with_locations(data, mime_type=_PDF)

    assert parse_document(data, mime_type=_PDF) == parsed.text
    assert [item.kind for item in parsed.locations] == ["page", "page", "page"]
    assert [item.number for item in parsed.locations] == [1, 2, 3]
    assert "Firstpagesentinel" in parsed.text.replace(" ", "")
    assert "Finalpagesentinel" in parsed.text.replace(" ", "")
    assert parsed.locations[0].char_start < parsed.locations[0].char_end
    assert parsed.locations[1].char_start == parsed.locations[1].char_end
    assert parsed.locations[2].char_start < parsed.locations[2].char_end
    _assert_location_bounds(parsed)


def test_pptx_locations_cover_slides_and_preserve_blank_slide_span() -> None:
    from app.ingestion.parsers import parse_document, parse_document_with_locations

    data = _make_presentation_with_blank_second_slide()
    parsed = parse_document_with_locations(data, mime_type=_PPTX)

    assert parse_document(data, mime_type=_PPTX) == parsed.text
    assert [item.kind for item in parsed.locations] == ["slide", "slide"]
    assert [item.number for item in parsed.locations] == [1, 2]
    assert any("Project plan" in item.name for item in parsed.locations)
    assert "Milestone detail" in parsed.text
    assert parsed.locations[1].char_start == parsed.locations[1].char_end
    _assert_location_bounds(parsed)


def test_xlsx_locations_use_sheet_names_and_preserve_blank_middle_sheet_span() -> None:
    from app.ingestion.parsers import parse_document, parse_document_with_locations

    data = _make_workbook_with_blank_middle_sheet()
    parsed = parse_document_with_locations(data, mime_type=_XLSX)

    assert parse_document(data, mime_type=_XLSX) == parsed.text
    assert [item.kind for item in parsed.locations] == ["sheet", "sheet", "sheet"]
    assert [item.name for item in parsed.locations] == ["North", "Blank Middle", "South"]
    assert [item.number for item in parsed.locations] == [1, 2, 3]
    assert "North" in parsed.text and "South" in parsed.text
    assert parsed.locations[1].char_start == parsed.locations[1].char_end
    _assert_location_bounds(parsed)


def test_chunk_crossing_pdf_pages_has_exact_text_and_both_locations() -> None:
    from app.ingestion.chunking import chunk_text
    from app.ingestion.parsers import parse_document_with_locations

    parsed = parse_document_with_locations(_make_pdf_pages_with_blank_middle(), mime_type=_PDF)
    chunks = chunk_text(parsed.text, chunk_size=256, overlap=0)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert parsed.text[chunk.char_start : chunk.char_end] == chunk.text
    mapped = parsed.locations_for_span(chunk.char_start, chunk.char_end)
    assert [item.number for item in mapped] == [1, 3]
    assert all(item.char_start < item.char_end for item in mapped)


@pytest.mark.parametrize(
    "overrides",
    [
        {"number": 0},
        {"char_end": -1},
        {"char_start": 2, "char_end": 1},
    ],
)
def test_source_location_rejects_invalid_numbers_and_spans(
    overrides: dict[str, int],
) -> None:
    source_location = import_module("app.domain.ingestion").SourceLocation
    values: dict[str, object] = {
        "kind": "page",
        "name": "Page 1",
        "number": 1,
        "char_start": 0,
        "char_end": 1,
        **overrides,
    }

    with pytest.raises(ValueError):
        source_location(**values)


def test_locations_for_span_rejects_negative_offsets() -> None:
    parse_document_with_locations = import_module(
        "app.ingestion.parsers"
    ).parse_document_with_locations
    parsed = parse_document_with_locations(_make_pdf_pages_with_blank_middle(), mime_type=_PDF)

    with pytest.raises(ValueError):
        parsed.locations_for_span(-1, 1)


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncIterator[None]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    import app.db.models  # noqa: F401 — register tables with Base.metadata
    import app.db.session as db_session
    from app.db.base import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    previous_engine = db_session._engine
    previous_sessionmaker = db_session._sessionmaker
    db_session._engine = engine
    db_session._sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield
    finally:
        db_session._engine = previous_engine
        db_session._sessionmaker = previous_sessionmaker
        await engine.dispose()


class _NoopIndexStore:
    @classmethod
    def from_settings(cls, _settings: object) -> _NoopIndexStore:
        return cls()

    async def ensure_index(self) -> None:
        return None

    async def upsert_chunks(self, _chunks: Sequence[object], *, refresh: bool = False) -> None:
        return None

    async def delete_document(
        self, *, tenant_id: uuid.UUID, document_id: uuid.UUID, refresh: bool = False
    ) -> None:
        return None

    async def aclose(self) -> None:
        return None


async def test_ingestion_persists_extracted_text_and_matching_part_maps(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    parse_document_with_locations = import_module(
        "app.ingestion.parsers"
    ).parse_document_with_locations
    db_session = import_module("app.db.session")
    index_sync = import_module("app.tasks.index_sync")
    repositories = import_module("app.db.repositories")
    ingest = import_module("app.tasks.ingest")
    from tests.test_ingestion_task import (
        _FakeGateway,
        _FakeObjectStore,
        _seed_document,
        _settings,
    )

    monkeypatch.setattr(index_sync, "OpenSearchStore", _NoopIndexStore)
    data = _make_pdf_pages_with_blank_middle()
    parsed = parse_document_with_locations(data, mime_type=_PDF)
    tenant_id, document_id = await _seed_document(mime_type=_PDF, key="pending")
    store = _FakeObjectStore()
    async with db_session.session_scope() as session:
        document = await repositories.DocumentRepository(session, tenant_id).get(document_id)
        assert document is not None
        store.put(str(tenant_id), document.storage_key, data)

    await ingest.ingest_document_async(
        tenant_id,
        document_id,
        settings=_settings(),
        object_store=store,
        gateway=_FakeGateway(),
    )

    async with db_session.session_scope() as session:
        document = await repositories.DocumentRepository(session, tenant_id).get(document_id)
        assert document is not None
        assert document.source_text == parsed.text
        assert document.source_locations == parsed.locations
        chunks = await repositories.ChunkRepository(session, tenant_id).list_for_document(
            document_id
        )
        assert chunks
        for chunk in chunks:
            assert chunk.source_locations == parsed.locations_for_span(
                chunk.char_start, chunk.char_end
            )
            assert parsed.text[chunk.char_start : chunk.char_end] == chunk.text

        indexed = index_sync._to_indexed(document, chunks)
        assert [item.source_locations for item in indexed] == [
            chunk.source_locations for chunk in chunks
        ]

        foreign_tenant_id = uuid.uuid4()
        assert (
            await repositories.DocumentRepository(session, foreign_tenant_id).get(document_id)
            is None
        )
        foreign_chunk_repo = repositories.ChunkRepository(session, foreign_tenant_id)
        assert await foreign_chunk_repo.get(chunks[0].id) is None
