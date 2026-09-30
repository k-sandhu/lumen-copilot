from __future__ import annotations

import io

import docx
import pytest

from app.ingestion.parsers import DocumentParseError, parse_document

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _serialize(document: docx.document.Document) -> bytes:
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def _parse(document: docx.document.Document) -> str:
    return parse_document(_serialize(document), mime_type=_DOCX)


def _make_table_document(
    rows: int, columns: int
) -> tuple[docx.document.Document, docx.table.Table]:
    document = docx.Document()
    table = document.add_table(rows=rows, cols=columns)
    return document, table


def test_docx_extracts_table_only_facts_with_header_labels_and_units() -> None:
    document, table = _make_table_document(2, 2)
    table.cell(0, 0).text = "Region"
    table.cell(0, 1).text = "Revenue (USD)"
    table.cell(1, 0).text = "West"
    table.cell(1, 1).text = "12"

    text = _parse(document)

    assert "[Table]" in text
    assert "Row 1:" in text and "C1=Region" in text
    assert "C2=Revenue (USD)" in text
    assert "Row 2:" in text
    assert "C1 [Region]=West" in text
    assert "C2 [Revenue (USD)]=12" in text
    assert "[/Table]" in text


def test_docx_preserves_paragraph_table_adjacency_order_and_paragraph_text() -> None:
    document = docx.Document()
    document.add_paragraph("Before table, unchanged.")
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "Middle table fact"
    document.add_paragraph("After table, unchanged.")

    text = _parse(document)

    before = text.index("Before table, unchanged.")
    table_start = text.index("[Table]")
    fact = text.index("Middle table fact")
    table_end = text.index("[/Table]")
    after = text.index("After table, unchanged.")
    assert before < table_start < fact < table_end < after
    assert "Before table, unchanged." in text
    assert "After table, unchanged." in text


def test_docx_table_keeps_blank_middle_grid_column() -> None:
    document, table = _make_table_document(2, 3)
    table.cell(0, 0).text = "Left"
    table.cell(0, 1).text = ""
    table.cell(0, 2).text = "Right"
    table.cell(1, 0).text = "A"
    table.cell(1, 1).text = ""
    table.cell(1, 2).text = "B"

    text = _parse(document)

    row = next(line for line in text.splitlines() if line.startswith("Row 2:"))
    assert "C1 [Left]=A" in row
    assert "C2" in row
    assert "C3 [Right]=B" in row


def test_docx_horizontally_merged_cells_keep_their_grid_positions() -> None:
    document, table = _make_table_document(2, 3)
    table.cell(0, 0).text = "Department"
    table.cell(0, 1).text = "Quarter"
    table.cell(0, 2).text = "Amount"
    merged = table.cell(1, 0).merge(table.cell(1, 1))
    merged.text = "North"
    table.cell(1, 2).text = "8"

    text = _parse(document)

    row = next(line for line in text.splitlines() if line.startswith("Row 2:"))
    assert "C1 [Department]=North" in row
    assert "C2 [Quarter]=North" in row
    assert "C3 [Amount]=8" in row


def test_docx_vertically_merged_cells_repeat_value_at_occupied_positions() -> None:
    document, table = _make_table_document(3, 2)
    table.cell(0, 0).text = "Team"
    table.cell(0, 1).text = "Owner"
    merged = table.cell(1, 0).merge(table.cell(2, 0))
    merged.text = "Platform"
    table.cell(1, 1).text = "Ari"
    table.cell(2, 1).text = "Bo"

    text = _parse(document)

    row_two = next(line for line in text.splitlines() if line.startswith("Row 2:"))
    row_three = next(line for line in text.splitlines() if line.startswith("Row 3:"))
    assert "C1 [Team]=Platform" in row_two
    assert "C1 [Team]=Platform" in row_three
    assert "C2 [Owner]=Ari" in row_two
    assert "C2 [Owner]=Bo" in row_three


def test_docx_horizontal_merge_marks_repeated_numeric_cell_origin() -> None:
    document, table = _make_table_document(2, 3)
    table.cell(0, 0).text = "Quantity"
    table.cell(0, 1).text = "Unit price"
    table.cell(0, 2).text = "Total"
    merged = table.cell(1, 0).merge(table.cell(1, 1))
    merged.text = "120"
    table.cell(1, 2).text = "240"

    text = _parse(document)

    row = next(line for line in text.splitlines() if line.startswith("Row 2:"))
    assert "C1 [Quantity]=120" in row
    assert "C2 [Unit price]=120 [merged from R2C1]" in row
    assert "C1 [Quantity]=120 [merged from" not in row
    assert "C3 [Total]=240 [merged from" not in row


def test_docx_vertical_merge_marks_repeated_numeric_cell_origin() -> None:
    document, table = _make_table_document(3, 2)
    table.cell(0, 0).text = "Amount"
    table.cell(0, 1).text = "Period"
    merged = table.cell(1, 0).merge(table.cell(2, 0))
    merged.text = "75"
    table.cell(1, 1).text = "Q1"
    table.cell(2, 1).text = "Q2"

    text = _parse(document)

    row_two = next(line for line in text.splitlines() if line.startswith("Row 2:"))
    row_three = next(line for line in text.splitlines() if line.startswith("Row 3:"))
    assert "C1 [Amount]=75" in row_two
    assert "C1 [Amount]=75 [merged from R2C1]" in row_three
    assert "C2 [Period]=Q2 [merged from" not in row_three


def test_docx_equal_unmerged_numeric_cells_are_not_marked_as_merged() -> None:
    document, table = _make_table_document(2, 2)
    table.cell(0, 0).text = "Count A"
    table.cell(0, 1).text = "Count B"
    table.cell(1, 0).text = "5"
    table.cell(1, 1).text = "5"

    text = _parse(document)

    row = next(line for line in text.splitlines() if line.startswith("Row 2:"))
    assert "C1 [Count A]=5" in row
    assert "C2 [Count B]=5" in row
    assert "[merged from" not in row


def test_docx_recursively_extracts_nested_table_text() -> None:
    document, table = _make_table_document(1, 1)
    cell = table.cell(0, 0)
    cell.paragraphs[0].text = "Outer cell"
    nested = cell.add_table(rows=1, cols=1)
    nested.cell(0, 0).text = "Nested secret code"

    text = _parse(document)

    assert "Outer cell" in text
    assert "Nested secret code" in text


def test_docx_preserves_paragraph_only_text_exactly() -> None:
    document = docx.Document()
    document.add_paragraph("First paragraph.")
    document.add_paragraph("Second paragraph.")

    assert _parse(document) == "First paragraph.\nSecond paragraph."


def test_corrupt_docx_still_raises_typed_parse_error() -> None:
    with pytest.raises(DocumentParseError):
        parse_document(b"not a DOCX zip container", mime_type=_DOCX)
