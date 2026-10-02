"""MIME-typed document parsing to plain text — dependency-localized (CC-5 #21).

Turns the stored bytes of an uploaded document into plain text for chunking +
embedding. Only the upload allowlist (spec 0004 / #22 ``UPLOAD_ALLOWED_CONTENT_TYPES``)
is supported: PDF, DOCX, PPTX, XLSX, ``text/plain``, ``text/markdown``.

**Dependency localization (ADR-0004 implementation note).** Each format's parser
library (``pypdf`` / ``python-docx`` / ``python-pptx`` / ``openpyxl``) is imported
**lazily, inside its small helper**, so:

* importing this module (or the task module, app boot, most tests) pulls in none
  of the parser libraries — the heavy/optional dependency stays contained here;
* the set of files that touch a given parser is exactly one function, so a
  swap/upgrade is localized.

**Fail-closed (AC-6 / INV-8).** Every failure path is a typed
:class:`DocumentParseError`: an unsupported MIME type, a corrupt/unreadable
file, or a DOCX extraction limit. The Celery task maps that to ``status=failed`` with the
reason — never a silent drop and never an unmapped crash. Parsing is pure CPU
work with no network and no I/O beyond the in-memory bytes it is handed.
"""

from __future__ import annotations

import io

# Upload-allowlist MIME types (kept in lockstep with #22's
# ``Settings.upload_allowed_content_types`` default; the task validates against
# the live setting, this is the parser-dispatch domain).
_PDF = "application/pdf"
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_TXT = "text/plain"
_MD = "text/markdown"

SUPPORTED_MIME_TYPES: frozenset[str] = frozenset({_PDF, _DOCX, _PPTX, _XLSX, _TXT, _MD})

# Hard bounds on the DOCX representation/traversal (spec 0010), not provider or
# deployment tuning. Check before expansion, and never return truncated success.
_DOCX_MAX_OUTPUT_CHARS = 2_000_000
_DOCX_MAX_WORK_UNITS = 100_000
_DOCX_MAX_TABLE_DEPTH = 32


class DocumentParseError(Exception):
    """Parsing failed — unsupported, unreadable or over extraction limits (AC-6).

    Carried by the task into ``Document.status=failed`` with the reason. A domain
    error, not an HTTP one; the task records it on the document row.
    """


class UnsupportedMimeTypeError(DocumentParseError):
    """The document's declared MIME type has no parser (outside the allowlist)."""


def _parse_text(data: bytes) -> str:
    """Decode plain text / markdown bytes as UTF-8 (lenient on stray bytes).

    ``errors="replace"`` keeps a stray non-UTF-8 byte from failing the whole
    ingest — the document is still mostly text and worth indexing; the rare
    replacement character is preferable to a hard failure for a text upload.
    """
    return data.decode("utf-8", errors="replace")


# The OOXML formats (docx/pptx/xlsx) are zip containers; corrupt/foreign bytes
# surface as a wide range of library-internal exceptions (BadZipFile, KeyError,
# lxml errors, …). Parsing *untrusted* upload bytes must never crash the worker,
# so each helper catches broadly and re-raises one typed DocumentParseError
# (AC-6); the original is chained for logs. ``MemoryError``/``KeyboardInterrupt``
# (BaseException) are deliberately not caught.


def _parse_pdf(data: bytes) -> str:
    """Extract text from a PDF, page by page (``pypdf``, imported lazily)."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 — untrusted bytes; mapped to a typed error
        raise DocumentParseError(f"could not parse PDF: {type(exc).__name__}") from exc
    return "\n\n".join(pages)


def _parse_docx(data: bytes) -> str:
    """Extract paragraphs and labelled table rows in body order (spec 0010)."""
    import docx
    from docx.document import Document as DocxDocument
    from docx.table import Table, _Cell
    from docx.text.paragraph import Paragraph

    output: list[str] = []
    output_chars = 0
    work_units = 0

    def charge_work(amount: int = 1) -> None:
        nonlocal work_units
        work_units += amount
        if work_units > _DOCX_MAX_WORK_UNITS:
            raise DocumentParseError("DOCX extraction work limit exceeded")

    def check_size(size: int) -> None:
        if size > _DOCX_MAX_OUTPUT_CHARS:
            raise DocumentParseError("DOCX extraction output limit exceeded")

    def emit(text: str) -> None:
        nonlocal output_chars
        check_size(output_chars + len(text))
        output_chars += len(text)
        output.append(text)

    def scalar_join(parts: list[str]) -> str:
        check_size(sum(map(len, parts)) + max(0, len(parts) - 1))
        return "\n".join(parts)

    def render_blocks(container: DocxDocument | _Cell, depth: int) -> None:
        for index, block in enumerate(container.iter_inner_content()):
            charge_work()
            if index:
                emit("\n")
            if isinstance(block, Paragraph):
                emit(block.text)
            elif isinstance(block, Table):
                render_table(block, depth + 1)

    def render_table(table: Table, depth: int) -> None:
        if depth > _DOCX_MAX_TABLE_DEPTH:
            raise DocumentParseError("DOCX extraction depth limit exceeded")
        emit("[Table]")
        headers: list[str] = []
        width = len(table.columns)
        charge_work(width)
        # Cache scalar rendering and origin metadata. Full nested content goes
        # straight to the bounded output once; aliases never materialize it.
        origins: dict[object, tuple[tuple[int, int], str, bool]] = {}
        previous: dict[int, _Cell] = {}
        for number, row in enumerate(table.rows, start=1):
            charge_work()
            # python-docx row.cells recursively follows every vertical merge.
            # Keep the preceding grid instead, visiting each physical cell once.
            grid_cells: dict[int, _Cell] = {}
            column = row.grid_cols_before + 1
            for tc in row._tr.tc_lst:
                charge_work(1 + tc.grid_span)
                grid_origin = previous[column] if tc.vMerge == "continue" else _Cell(tc, table)
                for occupied in range(column, column + tc.grid_span):
                    grid_cells[occupied] = grid_origin
                column += tc.grid_span
            previous = grid_cells
            row_width = max(width, column - 1 + row.grid_cols_after)
            charge_work(row_width)
            emit(f"\nRow {number}: ")
            for column in range(1, row_width + 1):
                cell = grid_cells.get(column)
                position = (number, column)
                origin, scalar, has_nested = position, "", False
                if cell is not None:
                    if cell._tc not in origins:
                        paragraphs: list[str] = []
                        scalar_chars = 0
                        for block in cell.iter_inner_content():
                            charge_work()
                            if isinstance(block, Paragraph):
                                text = block.text
                                scalar_chars += len(text) + bool(paragraphs)
                                check_size(scalar_chars)
                                paragraphs.append(text)
                            elif isinstance(block, Table):
                                has_nested = True
                        scalar = scalar_join(paragraphs)
                        if has_nested:
                            scalar = scalar.strip("\n")
                        origins[cell._tc] = (position, scalar, has_nested)
                    origin, scalar, has_nested = origins[cell._tc]
                nested_reference = (
                    f" [nested tables at R{origin[0]}C{origin[1]}]" if has_nested else ""
                )
                if number == 1:
                    check_size(len(scalar) + 2 * scalar.count("\n") + len(nested_reference))
                    headers.append(scalar.replace("\n", " / ") + nested_reference)
                label = (
                    f" [{headers[column - 1]}]"
                    if number > 1 and column <= len(headers) and headers[column - 1]
                    else ""
                )
                if column > 1:
                    emit(" | ")
                emit(f"C{column}{label}=")
                if origin != position:
                    emit(scalar)
                    emit(nested_reference)
                    emit(f" [merged from R{origin[0]}C{origin[1]}]")
                elif cell is not None:
                    render_blocks(cell, depth)
        emit("\n[/Table]")

    try:
        document = docx.Document(io.BytesIO(data))
        render_blocks(document, 0)
    except DocumentParseError:
        raise
    except Exception as exc:  # noqa: BLE001 — untrusted bytes; mapped to a typed error
        raise DocumentParseError(f"could not parse DOCX: {type(exc).__name__}") from exc
    return "".join(output)


def _parse_pptx(data: bytes) -> str:
    """Extract numbered slides, grouped text, tables and existing notes."""
    from pptx import Presentation
    from pptx.shapes.autoshape import Shape
    from pptx.shapes.graphfrm import GraphicFrame
    from pptx.shapes.group import GroupShape
    from pptx.shapes.shapetree import GroupShapes, SlideShapes

    def render_shapes(shapes: SlideShapes | GroupShapes) -> tuple[list[str], bool]:
        lines: list[str] = []
        has_content = False
        for shape in shapes:
            if isinstance(shape, GroupShape):
                group_lines, group_has_content = render_shapes(shape.shapes)
                lines.extend(group_lines)
                has_content = has_content or group_has_content
            elif isinstance(shape, GraphicFrame) and shape.has_table:
                lines.append("[Table]")
                headers: list[str] = []
                for number, row in enumerate(shape.table.rows, start=1):
                    values = ["" if cell.is_spanned else cell.text for cell in row.cells]
                    has_content = has_content or any(value.strip() for value in values)
                    if number == 1:
                        headers = [value.replace("\n", " / ") for value in values]
                    cells = []
                    for column, value in enumerate(values, start=1):
                        label = (
                            f" [{headers[column - 1]}]"
                            if number > 1 and headers[column - 1]
                            else ""
                        )
                        cells.append(f"C{column}{label}={value}")
                    lines.append(f"Row {number}: " + " | ".join(cells))
                lines.append("[/Table]")
            elif isinstance(shape, Shape) and shape.has_text_frame:
                text = shape.text_frame.text
                if text.strip():
                    lines.append(text)
                    has_content = True
        return lines, has_content

    try:
        presentation = Presentation(io.BytesIO(data))
        lines: list[str] = []
        for number, slide in enumerate(presentation.slides, start=1):
            content, has_content = render_shapes(slide.shapes)
            if slide.has_notes_slide:
                frame = slide.notes_slide.notes_text_frame
                if frame is not None and frame.text.strip():
                    content.extend([f"Notes (Slide {number}):", frame.text])
                    has_content = True
            if has_content:
                title_shape = slide.shapes.title
                title = title_shape.text if title_shape is not None else ""
                heading = f"Slide {number}" + (f": {title}" if title.strip() else "")
                lines.extend([heading, *content])
    except Exception as exc:  # noqa: BLE001 — untrusted bytes; mapped to a typed error
        raise DocumentParseError(f"could not parse PPTX: {type(exc).__name__}") from exc
    return "\n".join(lines)


def _parse_xlsx(data: bytes) -> str:
    """Render sheet names, labelled coordinates, formats and formula caches."""
    import zipfile
    from xml.etree import ElementTree

    from openpyxl import load_workbook
    from openpyxl.utils import coordinate_to_tuple, get_column_letter
    from openpyxl.worksheet.cell_range import CellRange
    from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

    def render_formula(expression: object) -> str:
        if isinstance(expression, str):
            return expression
        if isinstance(expression, ArrayFormula):
            return f"{expression.text or ''}; array range={expression.ref}"
        if isinstance(expression, DataTableFormula):
            # The pinned adapter yields source attributes in a fixed order.
            attributes = "; ".join(f"{key}={value}" for key, value in expression if key != "t")
            return f"dataTable; {attributes}"
        raise ValueError("unsupported XLSX formula type")

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            formulas = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
            try:
                sheets: list[str] = []
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    for worksheet, formula_sheet in zip(
                        workbook.worksheets, formulas.worksheets, strict=True
                    ):
                        merges: list[str] = []
                        merged_bounds: list[tuple[int, int, int, int]] = []
                        supplied_caches: set[str] = set()
                        # ReadOnlyWorksheet omits merged ranges. Validate them
                        # before either projection can expose covered values.
                        # It also decodes empty string caches as None, so retain
                        # cache presence/type directly from the source XML.
                        source_row = source_column = 0
                        source_coordinate = source_type = ""
                        has_formula = has_cache = False
                        with archive.open(worksheet._worksheet_path) as content:
                            for event, element in ElementTree.iterparse(
                                content, events=("start", "end")
                            ):
                                tag = element.tag.rpartition("}")[2]
                                if event == "start":
                                    if tag == "row":
                                        source_row = int(element.get("r", str(source_row + 1)))
                                        source_column = 0
                                    elif tag == "c":
                                        source_coordinate = element.get("r", "")
                                        if source_coordinate:
                                            _, source_column = coordinate_to_tuple(
                                                source_coordinate
                                            )
                                        else:
                                            source_column += 1
                                            source_coordinate = (
                                                f"{get_column_letter(source_column)}{source_row}"
                                            )
                                        source_type = element.get("t", "n")
                                        has_formula = has_cache = False
                                    continue
                                if tag == "f":
                                    has_formula = True
                                elif tag == "v":
                                    has_cache = element.text is not None or source_type == "str"
                                elif tag == "c" and has_formula and has_cache:
                                    supplied_caches.add(source_coordinate)
                                elif tag == "mergeCell":
                                    reference = element.attrib["ref"]
                                    merged_bounds.append(CellRange(reference).bounds)
                                    merges.append(reference)
                                element.clear()
                        rows: list[str] = []
                        headers: list[str] = []
                        for number, (row, formula_row) in enumerate(
                            zip(worksheet.iter_rows(), formula_sheet.iter_rows(), strict=True),
                            start=1,
                        ):
                            # Keep rectangles rather than expanding potentially
                            # large merges into a set of individual coordinates.
                            covered = [
                                any(
                                    min_row <= number <= max_row
                                    and min_col <= column <= max_col
                                    and (number, column) != (min_row, min_col)
                                    for min_col, min_row, max_col, max_row in merged_bounds
                                )
                                for column in range(1, len(formula_row) + 1)
                            ]
                            if not any(
                                cell.value is not None and not covered[column]
                                for column, cell in enumerate(formula_row)
                            ):
                                continue
                            first = not headers
                            if first:
                                headers = [
                                    str(cell.value)
                                    if cell.value is not None and not covered[column]
                                    else ""
                                    for column, cell in enumerate(row)
                                ]
                            cells: list[str] = []
                            for column, (cell, formula) in enumerate(
                                zip(row, formula_row, strict=True), start=1
                            ):
                                coordinate = f"{get_column_letter(column)}{number}"
                                if covered[column - 1]:
                                    cells.append(f"{coordinate}=")
                                    continue
                                label = (
                                    f" [{headers[column - 1]}]"
                                    if not first and headers[column - 1]
                                    else ""
                                )
                                value = str(cell.value) if cell.value is not None else ""
                                if cell.value is not None and cell.number_format != "General":
                                    value += f" [format={cell.number_format}]"
                                if formula.data_type == "f":
                                    cache = (
                                        "cached value unavailable"
                                        if coordinate not in supplied_caches
                                        else "cached value supplied; freshness unknown"
                                    )
                                    value += f" [formula={render_formula(formula.value)}; {cache}]"
                                cells.append(f"{coordinate}{label}={value}")
                            rows.append(
                                f"Row {number} (Sheet {worksheet.title}): " + " | ".join(cells)
                            )
                        if rows:
                            heading = [f"Sheet: {worksheet.title}"]
                            if merges:
                                heading.append("Merged cells: " + ", ".join(merges))
                            sheets.append("\n".join(heading + rows))
            finally:
                formulas.close()
        finally:
            workbook.close()
    except Exception as exc:  # noqa: BLE001 — untrusted bytes; mapped to a typed error
        raise DocumentParseError(f"could not parse XLSX: {type(exc).__name__}") from exc
    return "\n\n".join(sheets)


# MIME type -> parser. Each value localizes its library import to its own body.
_PARSERS = {
    _PDF: _parse_pdf,
    _DOCX: _parse_docx,
    _PPTX: _parse_pptx,
    _XLSX: _parse_xlsx,
    _TXT: _parse_text,
    _MD: _parse_text,
}


def parse_document(data: bytes, *, mime_type: str) -> str:
    """Extract plain text from ``data`` for the allowlisted ``mime_type``.

    Dispatches on the (normalized) MIME type to the matching helper. The leading
    parameter of a ``Content-Type`` (e.g. ``text/plain; charset=utf-8``) is
    tolerated by splitting on ``;``.

    Raises:
        UnsupportedMimeTypeError: ``mime_type`` is outside the allowlist (AC-6).
        DocumentParseError: corrupt/unreadable bytes or a DOCX extraction limit.
    """
    normalized = mime_type.split(";", 1)[0].strip().lower()
    parser = _PARSERS.get(normalized)
    if parser is None:
        raise UnsupportedMimeTypeError(f"unsupported MIME type for ingestion: {normalized!r}")
    return parser(data)
