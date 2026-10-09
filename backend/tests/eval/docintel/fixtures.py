"""Small reproducible documents built from code, without third-party content."""

from __future__ import annotations

import gzip
import hashlib
import io
import struct
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import datetime

from tests.eval.docintel.metrics import Gold


@dataclass(frozen=True)
class Fixture:
    id: str
    format: str
    mime: str
    data: bytes
    gold: Gold
    language: str = "en"
    case: str = "structured"
    expected: str = "indexed"
    fidelity_eligible: bool = True

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def _zip(files: dict[str, bytes | str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        names = sorted(files, key=lambda name: (name != "mimetype", name))
        for name in names:
            data = files[name]
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            archive.writestr(info, data)
    return out.getvalue()


def _stable_office(data: bytes) -> bytes:
    import re

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    if "docProps/core.xml" in files:
        files["docProps/core.xml"] = re.sub(
            rb"(<dcterms:(?:created|modified)[^>]*>).*?(</dcterms:(?:created|modified)>)",
            rb"\g<1>1980-01-01T00:00:00Z\g<2>",
            files["docProps/core.xml"],
        )
    for name, value in list(files.items()):
        if name.startswith("ppt/embeddings/") and value.startswith(b"PK"):
            files[name] = _stable_office(value)
    return _zip(files)


def _raster_fact() -> tuple[int, int, bytes]:
    # Original bitmap glyphs author the visible gold word FACT. No image assets.
    glyphs = (
        "111/100/110/100/100",
        "010/101/111/101/101",
        "111/100/100/100/111",
        "111/010/010/010/010",
    )
    scale = 8
    rows = ["0".join(glyph.split("/")[row] for glyph in glyphs) for row in range(5)]
    pixels = bytes(
        value
        for row in rows
        for _ in range(scale)
        for bit in row
        for value in [0 if bit == "1" else 255] * scale
    )
    return len(rows[0]) * scale, len(rows) * scale, pixels


def _pdf(*, scanned: bool = False, mixed: bool = False) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
    )

    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for index in range(3):
        page = writer.add_blank_page(width=612, height=792)
        if index == 2:  # an intentional blank native page
            continue
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        if scanned and (not mixed or index == 1):
            # Our own 1-bit image marks. It has no text layer and requires OCR.
            image = DecodedStreamObject()
            width, height, pixels = _raster_fact()
            image.set_data(pixels)
            image.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(width),
                    NameObject("/Height"): NumberObject(height),
                    NameObject("/BitsPerComponent"): NumberObject(8),
                    NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                }
            )
            page[NameObject("/Resources")][NameObject("/XObject")] = DictionaryObject(
                {NameObject("/Im1"): writer._add_object(image)}
            )
            stream.set_data(b"q 300 0 0 100 50 650 cm /Im1 Do Q")
        else:
            # Two columns, a fact-bearing row, and a second native page.
            lines = (
                [(50, 740, "Intro"), (50, 710, "North -120 kg"), (340, 740, "RightColumn")]
                if index == 0
                else [(50, 740, "PageTwo")]
            )
            stream.set_data(
                "\n".join(
                    f"BT /F1 12 Tf {x} {y} Td ({text}) Tj ET" for x, y, text in lines
                ).encode()
            )
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Annots")] = ArrayObject()
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _docx() -> bytes:
    from docx import Document

    doc = Document()
    doc.add_heading("Intro", 1)
    table = doc.add_table(rows=3, cols=3)
    for cell, text in zip(table.rows[0].cells, ("Region", "Mass", "Unit"), strict=True):
        cell.text = text
    for cell, text in zip(table.rows[1].cells, ("North", "-120", "kg"), strict=True):
        cell.text = text
    table.cell(2, 0).merge(table.cell(2, 1)).text = "Merged"
    nested = table.cell(2, 2).add_table(rows=1, cols=1)
    nested.cell(0, 0).text = "Nested"
    doc.add_paragraph("PageTwo")
    doc.add_paragraph("مرحبا 世界 😀")
    out = io.BytesIO()
    doc.save(out)
    return _stable_office(out.getvalue())


def _pptx() -> bytes:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1)).text = "Intro"
    group = slide.shapes.add_group_shape()
    group.shapes.add_textbox(Inches(1), Inches(2), Inches(3), Inches(1)).text = "Grouped"
    table = slide.shapes.add_table(2, 3, Inches(1), Inches(3), Inches(5), Inches(1)).table
    for col, text in enumerate(("Region", "Mass", "Unit")):
        table.cell(0, col).text = text
    for col, text in enumerate(("North", "-120", "kg")):
        table.cell(1, col).text = text
    slide.notes_slide.notes_text_frame.text = "SpeakerFact"
    chart = CategoryChartData()
    chart.categories = ["North"]
    chart.add_series("Mass", (-120,))
    slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(6), Inches(1), Inches(3), Inches(3), chart
    )
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1)).text = "PageTwo"
    deck.slides.add_slide(deck.slide_layouts[6])
    out = io.BytesIO()
    deck.save(out)
    return _stable_office(out.getvalue())


def _xlsx() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.properties.created = datetime(1980, 1, 1)
    workbook.properties.modified = datetime(1980, 1, 1)
    sheet = workbook.active
    sheet.title = "Intro"
    sheet.append(("Region", "Mass", "Unit"))
    sheet.append(("North", -120, "kg"))
    sheet["E10"] = "Sparse"
    sheet.merge_cells("A4:B4")
    sheet["A4"] = "Merged"
    sheet["F10"] = "=B2*2"
    workbook.create_sheet("SheetTwo").append(("PageTwo", "世界"))
    workbook.create_sheet("Blank")
    out = io.BytesIO()
    workbook.save(out)
    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as archive:
        files = {n: archive.read(n) for n in archive.namelist()}
    files["xl/worksheets/sheet1.xml"] = files["xl/worksheets/sheet1.xml"].replace(
        b"<f>B2*2</f><v></v>", b"<f>B2*2</f><v>-240</v>"
    )
    return _stable_office(_zip(files))


def _odf(kind: str) -> bytes:
    mime = {
        "odt": "application/vnd.oasis.opendocument.text",
        "ods": "application/vnd.oasis.opendocument.spreadsheet",
        "odp": "application/vnd.oasis.opendocument.presentation",
    }[kind]
    row = (
        "<table:table-row>"
        + "".join(
            f'<table:table-cell office:value-type="string"><text:p>{v}</text:p></table:table-cell>'
            for v in ("North", "-120", "kg")
        )
        + "</table:table-row>"
    )
    table = f'<table:table table:name="Intro">{row}</table:table>'
    content = {
        "odt": '<office:text><text:h text:outline-level="1">Intro</text:h>'
        + table
        + "<text:p>PageTwo</text:p></office:text>",
        "ods": "<office:spreadsheet>"
        + table
        + (
            '<table:table table:name="SheetTwo"><table:table-row><table:t'
            'able-cell office:value-type="string"><text:p>PageTwo</text:p'
            "></table:table-cell></table:table-row></table:table></office"
            ":spreadsheet>"
        ),
        "odp": (
            '<office:presentation><draw:page draw:name="Intro"><draw:fram'
            "e><draw:text-box><text:p>Intro</text:p><text:p>North -120 kg"
            "</text:p></draw:text-box></draw:frame></draw:page><draw:page"
            ' draw:name="PageTwo"/></office:presentation>'
        ),
    }[kind]
    xml = (
        f'<?xml version="1.0"?><office:document-content xmlns:office="'
        f'urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text'
        f'="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:tabl'
        f'e="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:dr'
        f'aw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" offic'
        f'e:version="1.2"><office:body>{content}</office:body></office'
        f":document-content>"
    )
    manifest = (
        f'<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendo'
        f'cument:xmlns:manifest:1.0"><manifest:file-entry manifest:ful'
        f'l-path="/" manifest:media-type="{mime}"/><manifest:file-entr'
        f'y manifest:full-path="content.xml" manifest:media-type="text'
        f'/xml"/></manifest:manifest>'
    )
    return _zip({"mimetype": mime, "content.xml": xml, "META-INF/manifest.xml": manifest})


def _cfb(stream_name: str) -> bytes:
    # A valid compound-file envelope for recognition, NOT a legacy document.
    free, end, fat = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
    header = bytearray(512)
    header[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<HHHH", header, 24, 0x3E, 3, 0xFFFE, 9)
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into("<IIIIIIIII", header, 40, 0, 1, 0, 0, 4096, end, 0, end, 0)
    struct.pack_into("<109I", header, 76, 9, *([free] * 108))

    def entry(name: str, typ: int, child: int, start: int, size: int) -> bytes:
        b = bytearray(128)
        raw = (name + "\0").encode("utf-16-le")
        b[: len(raw)] = raw
        struct.pack_into("<HBBIII", b, 64, len(raw), typ, 1, free, free, child)
        struct.pack_into("<IQ", b, 116, start, size)
        return bytes(b)

    directory = (
        entry("Root Entry", 5, 1, end, 0) + entry(stream_name, 2, free, 1, 4096) + bytes(256)
    )
    sectors = [end] + list(range(2, 9)) + [end, fat] + [free] * 118
    return bytes(header) + directory + bytes(4096) + struct.pack("<128I", *sectors)


def corpus() -> list[Fixture]:
    gold = Gold(
        facts=("Intro", "North", "-120", "kg", "PageTwo"),
        associations=(("North", "-120", "kg"),),
        order=("Intro", "North", "PageTwo"),
    )
    result = [
        Fixture("pdf-columns", "pdf", "application/pdf", _pdf(), gold),
        Fixture(
            "pdf-scanned",
            "pdf",
            "application/pdf",
            _pdf(scanned=True),
            Gold(facts=("FACT",), native_regions=3),
            case="scanned",
        ),
        Fixture(
            "pdf-mixed",
            "pdf",
            "application/pdf",
            _pdf(scanned=True, mixed=True),
            Gold(facts=("Intro", "FACT"), native_regions=3),
            case="mixed",
        ),
        Fixture(
            "docx-nested",
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            _docx(),
            Gold(
                facts=gold.facts + ("Merged", "Nested", "مرحبا", "世界"),
                associations=gold.associations,
                order=gold.order,
                headers=(("Region", "North"), ("Mass", "-120")),
            ),
            language="mixed",
        ),
        Fixture(
            "pptx-groups-notes",
            "pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            _pptx(),
            Gold(
                facts=gold.facts + ("Grouped", "SpeakerFact"),
                associations=gold.associations,
                order=gold.order,
                native_regions=3,
            ),
        ),
        Fixture(
            "xlsx-sparse-formula",
            "xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            _xlsx(),
            Gold(
                facts=("North", "-120", "kg", "Sparse", "-240", "SheetTwo", "PageTwo"),
                associations=gold.associations,
                order=("North", "SheetTwo"),
                native_regions=3,
            ),
        ),
    ]
    for kind in ("odt", "ods", "odp"):
        result.append(
            Fixture(
                kind + "-parts",
                kind,
                {
                    "odt": "application/vnd.oasis.opendocument.text",
                    "ods": "application/vnd.oasis.opendocument.spreadsheet",
                    "odp": "application/vnd.oasis.opendocument.presentation",
                }[kind],
                _odf(kind),
                gold,
            )
        )
    text = "Intro\nNorth -120 kg\nPageTwo"
    epub = _zip(
        {
            "mimetype": "application/epub+zip",
            "META-INF/container.xml": (
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:cont'
                'ainer"><rootfiles><rootfile full-path="book.opf" media-type='
                '"application/oebps-package+xml"/></rootfiles></container>'
            ),
            "book.opf": (
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
                'unique-identifier="book"><metadata xmlns:dc="http://purl.org'
                '/dc/elements/1.1/"><dc:identifier id="book">fixture</dc:iden'
                "tifier><dc:title>Generated</dc:title><dc:language>en</dc:lan"
                'guage></metadata><manifest><item id="a" href="a.xhtml" media'
                '-type="application/xhtml+xml"/><item id="b" href="b.xhtml" m'
                'edia-type="application/xhtml+xml"/></manifest><spine><itemre'
                'f idref="a"/><itemref idref="b"/></spine></package>'
            ),
            "a.xhtml": (
                '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                "<h1>Intro</h1><p>North -120 kg</p></body></html>"
            ),
            "b.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>PageTwo</p></body></html>',
        }
    )
    samples = {
        "epub": ("application/epub+zip", epub),
        "csv": ("text/csv", b'Intro,Mass,Unit\nNorth,-120,kg\n"PageTwo\nquoted",0,kg'),
        "tsv": ("text/tab-separated-values", b"Intro\tMass\tUnit\nNorth\t-120\tkg\nPageTwo\t0\tkg"),
        "json": (
            "application/json",
            b'{"Intro":{"North":{"mass":-120,"unit":"kg"}},"PageTwo":true}',
        ),
        "jsonl": ("application/x-ndjson", b'{"Intro":"North -120 kg"}\n{"PageTwo":true}'),
        "xml": ("application/xml", b"<root><Intro>North -120 kg</Intro><PageTwo/></root>"),
        "xbrl": (
            "application/xbrl+xml",
            (
                b'<xbrl xmlns="http://www.xbrl.org/2003/instance"><context id='
                b'"North"><entity><identifier scheme="fixture">Intro</identifi'
                b"er></entity><period><instant>2026-01-01</instant></period></"
                b'context><unit id="kg"><measure>kg</measure></unit><mass cont'
                b'extRef="North" unitRef="kg">-120</mass><PageTwo/></xbrl>'
            ),
        ),
        "ixbrl": (
            "application/xhtml+xml",
            (
                b'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://'
                b'www.xbrl.org/2013/inlineXBRL"><body><h1>Intro</h1><p>North <'
                b'ix:nonFraction name="Mass" contextRef="North" unitRef="kg">-'
                b"120</ix:nonFraction> kg</p><p>PageTwo</p></body></html>"
            ),
        ),
        "ipynb": (
            "application/x-ipynb+json",
            (
                b'{"nbformat":4,"nbformat_minor":5,"metadata":{},"cells":[{"ce'
                b'll_type":"markdown","metadata":{},"source":["Intro\\nNorth -1'
                b'20 kg\\nPageTwo"]},{"cell_type":"code","metadata":{},"executi'
                b'on_count":null,"source":["raise RuntimeError("never execute"'
                b')"],"outputs":[]}]}'
            ),
        ),
        "eml": (
            "message/rfc822",
            (
                b"From: generated@example.invalid\r\nTo: reader@example.invalid\r"
                b"\nSubject: Intro\r\nMIME-Version: 1.0\r\nContent-Type: multipart/"
                b"mixed; boundary=fixture\r\n\r\n--fixture\r\nContent-Type: text/pla"
                b"in\r\n\r\nNorth -120 kg\r\nPageTwo\r\n--fixture\r\nContent-Type: text/"
                b'plain\r\nContent-Disposition: attachment; filename="fact.txt"\r'
                b"\n\r\nAttachmentFact\r\n--fixture--\r\n"
            ),
        ),
        "mbox": (
            "application/mbox",
            (
                b"From generated@example.invalid Thu Jan  1 00:00:00 1970\nFrom"
                b": generated@example.invalid\nSubject: Intro\n\nNorth -120 kg\nPa"
                b"geTwo\n"
            ),
        ),
        "mhtml": (
            "multipart/related",
            (
                b"MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary"
                b"=fixture\r\n\r\n--fixture\r\nContent-Type: text/html\r\n\r\n<h1>Intro<"
                b"/h1><p>North -120 kg</p><p>PageTwo</p>\r\n--fixture--\r\n"
            ),
        ),
        "rtf": ("application/rtf", b"{\\rtf1\\ansi Intro\\par North -120 kg\\par PageTwo}"),
        "text": ("text/plain", text.encode()),
        "markdown": ("text/markdown", ("# " + text).encode()),
    }
    for kind, (mime, data) in samples.items():
        annotated = (
            Gold(
                facts=gold.facts + ("AttachmentFact",),
                associations=gold.associations,
                order=gold.order + ("AttachmentFact",),
            )
            if kind == "eml"
            else gold
        )
        result.append(Fixture(kind + "-facts", kind, mime, data, annotated))
    result.extend(
        [
            Fixture(
                "text-utf16",
                "text",
                "text/plain",
                ("Intro\nNorth -120 kg\nPageTwo 世界").encode("utf-16"),
                gold,
                language="mixed",
                case="encoding",
            ),
            Fixture(
                "text-windows1252",
                "text",
                "text/plain",
                ("Intro\nNorth -120 kg\nPageTwo café").encode("cp1252"),
                gold,
                language="fr",
                case="encoding",
            ),
            Fixture(
                "text-empty", "text", "text/plain", b"", Gold(), case="blank", expected="empty"
            ),
        ]
    )
    archive = _zip({"intro.txt": text, "nested.zip": _zip({"fact.txt": "AttachmentFact"})})
    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        info = tarfile.TarInfo("intro.txt")
        info.size = len(text.encode())
        info.mtime = 0
        tf.addfile(info, io.BytesIO(text.encode()))
    for kind, mime, data in (
        ("zip", "application/zip", archive),
        ("tar", "application/x-tar", tar.getvalue()),
        ("gzip", "application/gzip", gzip.compress(text.encode(), mtime=0)),
    ):
        annotated = (
            Gold(
                facts=gold.facts + ("AttachmentFact",),
                associations=gold.associations,
                order=gold.order + ("AttachmentFact",),
            )
            if kind == "zip"
            else gold
        )
        result.append(Fixture(kind + "-nested", kind, mime, data, annotated))
    for kind, stream in (
        ("doc", "WordDocument"),
        ("xls", "Workbook"),
        ("ppt", "PowerPoint Document"),
        ("msg", "__properties_version1.0"),
    ):
        result.append(
            Fixture(
                kind + "-recognition",
                kind,
                "application/octet-stream",
                _cfb(stream),
                Gold(),
                case="recognition-only",
                expected="unsupported",
                fidelity_eligible=False,
            )
        )
    # Our own pixels, generated as a PNG through stdlib (no external images).
    import zlib

    def png_chunk(name: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))
        )

    width, height, pixels = _raster_fact()
    raw = b"".join(b"\0" + pixels[y * width : (y + 1) * width] for y in range(height))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(raw))
        + png_chunk(b"IEND", b"")
    )
    result.append(
        Fixture("image-pixels", "image", "image/png", png, Gold(facts=("FACT",)), case="scanned")
    )
    result.extend(
        [
            Fixture(
                "pdf-corrupt",
                "pdf",
                "application/pdf",
                b"%PDF-1.7 broken",
                Gold(),
                case="corrupt",
                expected="failed",
            ),
            Fixture(
                "zip-traversal",
                "zip",
                "application/zip",
                _zip({"../outside.txt": "must reject"}),
                Gold(),
                case="unsafe-path",
                expected="failed",
            ),
            Fixture(
                "xml-entity",
                "xml",
                "application/xml",
                b'<!DOCTYPE root [<!ENTITY x SYSTEM "file:///must-not-read">]><root>&x;</root>',
                Gold(),
                case="entity",
                expected="failed",
            ),
        ]
    )
    result.extend(
        [
            Fixture(
                "html-table",
                "html",
                "text/html",
                (
                    b"<h1>Intro</h1><table><tr><th>Region</th><th>Mass</th><th>Unit</th>"
                    b"</tr><tr><td>North</td><td>-120</td><td>kg</td></tr></table><p>PageTwo</p>"
                ),
                gold,
            ),
            Fixture(
                "xhtml-facts",
                "xhtml",
                "application/xhtml+xml",
                (
                    b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Intro</p>'
                    b"<p>North -120 kg</p><p>PageTwo</p></body></html>"
                ),
                gold,
            ),
            Fixture(
                "text-multilingual",
                "text",
                "text/plain",
                "مرحبا\nשלום\nहिन्दी\n世界\n😀".encode(),
                Gold(facts=("مرحبا", "שלום", "हिन्दी", "世界", "😀")),
                language="mixed",
                case="unicode",
            ),
        ]
    )
    return result
