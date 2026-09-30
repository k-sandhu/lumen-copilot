from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree

import pytest
from openpyxl import Workbook

from app.ingestion.parsers import DocumentParseError, parse_document

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _serialize(workbook: Workbook) -> bytes:
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _parse(workbook: Workbook) -> str:
    return parse_document(_serialize(workbook), mime_type=_XLSX)


def _set_cached_value(data: bytes, coordinate: str, value: str) -> bytes:
    source = io.BytesIO(data)
    output = io.BytesIO()
    with (
        zipfile.ZipFile(source) as archive,
        zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as updated,
    ):
        for item in archive.infolist():
            contents = archive.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                root = ElementTree.fromstring(contents)
                namespace = root.tag.partition("}")[0] + "}"
                cell = root.find(f".//{namespace}c[@r='{coordinate}']")
                assert cell is not None
                cached = cell.find(f"{namespace}v")
                assert cached is not None
                cached.text = value
                contents = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
            updated.writestr(item, contents)
    return output.getvalue()


def test_xlsx_renders_each_nonempty_sheet_and_repeats_header_units() -> None:
    workbook = Workbook()
    sales = workbook.active
    sales.title = "Sales"
    sales.append(["Region", "Revenue (USD)"])
    sales.append(["West", 12])
    costs = workbook.create_sheet("Costs")
    costs.append(["Category", "Cost (CAD)"])
    costs.append(["Travel", 5])

    text = _parse(workbook)

    assert "Sheet: Sales" in text
    assert "Row 2 (Sheet Sales):" in text
    assert "A2 [Region]=West" in text
    assert "B2 [Revenue (USD)]=12" in text
    assert "Sheet: Costs" in text
    assert "Row 2 (Sheet Costs):" in text
    assert "A2 [Category]=Travel" in text
    assert "B2 [Cost (CAD)]=5" in text


def test_xlsx_keeps_column_coordinates_across_gaps_and_blank_cells() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Gaps"
    sheet["A1"] = "Left"
    sheet["C1"] = "Right"
    sheet["A2"] = "alpha"
    sheet["C2"] = "gamma"
    sheet["A4"] = "next row"
    sheet["C4"] = "still column C"

    text = _parse(workbook)

    row_two = next(line for line in text.splitlines() if line.startswith("Row 2 (Sheet Gaps):"))
    assert "A2 [Left]=alpha" in row_two
    assert "C2 [Right]=gamma" in row_two
    assert row_two.index("A2") < row_two.index("B2") < row_two.index("C2")
    assert "Row 3 (Sheet Gaps):" not in text
    row_four = next(line for line in text.splitlines() if line.startswith("Row 4 (Sheet Gaps):"))
    assert "C4" in row_four


def test_xlsx_preserves_zero_and_false_as_values() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Flags"
    sheet.append(["Count", "Enabled"])
    sheet.append([0, False])

    text = _parse(workbook)

    row = next(line for line in text.splitlines() if line.startswith("Row 2 (Sheet Flags):"))
    assert "A2 [Count]=0" in row
    assert "B2 [Enabled]=False" in row


def test_xlsx_retains_non_general_percent_number_format() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Rates"
    sheet.append(["Completion"])
    sheet["A2"] = 0.25
    sheet["A2"].number_format = "0.00%"

    text = _parse(workbook)

    assert "A2 [Completion]=0.25 [format=0.00%]" in text


def test_xlsx_reports_merged_ranges_and_leaves_covered_cells_blank() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Merged"
    sheet.merge_cells("A1:B1")
    sheet["A1"] = "Combined heading"
    sheet["A2"] = "Detail"
    sheet["B2"] = "Value"
    sheet["A3"] = "item"
    sheet["B3"] = 7

    text = _parse(workbook)

    assert "Merged cells: A1:B1" in text
    assert "A1=Combined heading" in text
    assert "B1=Combined heading" not in text
    assert "B1=" in text  # the covered position is retained, without a second value


def test_xlsx_marks_formula_when_cached_value_is_unavailable() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Totals"
    sheet.append(["Amount"])
    sheet.append([2])
    sheet.append([3])
    sheet["A4"] = "=SUM(A2:A3)"

    text = _parse(workbook)

    row = next(line for line in text.splitlines() if line.startswith("Row 4 (Sheet Totals):"))
    assert "A4 [Amount]" in row
    assert "[formula==SUM(A2:A3); cached value unavailable]" in row


def test_xlsx_marks_formula_and_supplied_cached_value_as_freshness_unknown() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Totals"
    sheet.append(["Amount"])
    sheet.append([2])
    sheet.append([3])
    sheet["A4"] = "=SUM(A2:A3)"
    data = _set_cached_value(_serialize(workbook), "A4", "5")

    text = parse_document(data, mime_type=_XLSX)

    row = next(line for line in text.splitlines() if line.startswith("Row 4 (Sheet Totals):"))
    assert "A4 [Amount]=5" in row
    assert "[formula==SUM(A2:A3); cached value supplied; freshness unknown]" in row


def test_xlsx_with_no_nonblank_cells_returns_empty_text() -> None:
    workbook = Workbook()
    workbook.active.title = "Empty"

    assert _parse(workbook) == ""


def test_corrupt_xlsx_raises_typed_parse_error() -> None:
    with pytest.raises(DocumentParseError):
        parse_document(b"not an XLSX workbook", mime_type=_XLSX)
