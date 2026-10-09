"""Original generated PDF 1.5+ and Unicode documents; no external content."""
# ruff: noqa: E501 -- PDF syntax literals keep generated object topology readable.

from __future__ import annotations

import re
import struct
import zlib

from tests.eval.docintel.fixtures import Fixture
from tests.eval.docintel.pdf_fixtures import document, objects, text


def object_stream() -> bytes:
    values = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
    ]
    body = b""
    pairs = []
    for index, value in enumerate(values, 1):
        pairs.append(f"{index} {len(body)} ".encode())
        body += value + b" "
    header = b"".join(pairs)
    compressed = zlib.compress(header + body)
    content = text(40, 700, 12, "Object stream text")
    data = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for index, value in (
        (5, f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream"),
        (
            6,
            f"<< /Type /ObjStm /N 4 /First {len(header)} /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n".encode()
            + compressed
            + b"\nendstream",
        ),
    ):
        offsets[index] = len(data)
        data.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    offsets[7] = len(data)
    entries = [struct.pack("!BIH", 0, 0, 65535)]
    entries += [struct.pack("!BIH", 2, 6, i) for i in range(4)]
    entries += [struct.pack("!BIH", 1, offsets[i], 0) for i in (5, 6, 7)]
    stream = zlib.compress(b"".join(entries))
    data.extend(
        f"7 0 obj\n<< /Type /XRef /Root 1 0 R /Size 8 /W [1 4 2] /Length {len(stream)} /Filter /FlateDecode >>\nstream\n".encode()
        + stream
        + b"\nendstream\nendobj\n"
    )
    data.extend(f"startxref\n{offsets[7]}\n%%EOF\n".encode())
    return bytes(data)


def incremental() -> bytes:
    data = document([text(40, 700, 12, "Superseded text")])
    previous = re.findall(rb"startxref\s+(\d+)", data)[-1].decode()
    content = text(40, 700, 12, "Latest revision")
    offset = len(data)
    replacement = (
        f"5 0 obj\n<< /Length {len(content)} >>\nstream\n".encode()
        + content
        + b"\nendstream\nendobj\n"
    )
    return (
        data
        + replacement
        + (
            f"xref\n5 1\n{offset:010} 00000 n \ntrailer\n<< /Size 6 /Root 1 0 R /Prev {previous} >>\n"
            f"startxref\n{offset+len(replacement)}\n%%EOF\n"
        ).encode()
    )


def cid(value: str, *, rotation: int = 0, unmapped: bool = False) -> bytes:
    # Every input Unicode scalar gets a CID and a ToUnicode mapping; supplementary
    # scalars use a UTF-16 surrogate pair inside one mapping.
    mappings = b"\n".join(
        f"<{i:04x}> <{c.encode('utf-16-be').hex()}>".encode() for i, c in enumerate(value, 1)
    )
    cmap = (
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n/CMapName /Original-UCS def /CMapType 2 def\n1 begincodespacerange\n<0000> <ffff>\nendcodespacerange\n"
        + f"{len(value)} beginbfchar\n".encode()
        + mappings
        + b"\nendbfchar\nendcmap CMapName currentdict /CMap defineresource pop end end"
    )
    indices = list(range(1, len(value) + 1))
    if all(0x0590 <= ord(c) <= 0x08FF for c in value):
        # Paint visual order left-to-right. PDFium's bidi pass yields logical text.
        indices.reverse()
    encoded = "".join(f"{i:04x}" for i in indices)
    content = f"BT /F1 12 Tf 1 0 0 1 40 700 Tm <{encoded}> Tj ET".encode()
    return objects(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            b"<< /Type /Font /Subtype /Type0 /BaseFont /Original /Encoding /Identity-H /DescendantFonts [6 0 R] "
            + (b"" if unmapped else b"/ToUnicode 7 0 R ")
            + b">>",
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Rotate {rotation} /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>".encode(),
            f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
            b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Original /CIDSystemInfo << /Registry (Original) /Ordering (Identity) /Supplement 0 >> /FontDescriptor 8 0 R /DW 600 /CIDToGIDMap /Identity >>",
            f"<< /Length {len(cmap)} >>\nstream\n".encode() + cmap + b"\nendstream",
            b"<< /Type /FontDescriptor /FontName /Original /Flags 32 /FontBBox [0 -200 1000 800] /ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 >>",
        ]
    )


def annotations() -> bytes:
    content = text(40, 700, 12, "Page evidence")
    appearance = text(40, 600, 12, "Excluded widget")
    return objects(
        [
            b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [6 0 R] >> >>",
            b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R /Annots [6 0 R] >>",
            f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
            b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (Field) /V (Excluded value) /Rect [40 590 200 620] /AP << /N 7 0 R >> >>",
            f"<< /Type /XObject /Subtype /Form /BBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Length {len(appearance)} >>\nstream\n".encode()
            + appearance
            + b"\nendstream",
        ]
    )


def corpus() -> list[Fixture]:
    from tests.eval.docintel.metrics import Gold

    cases = [
        ("object-xref-streams", object_stream(), "Object stream text"),
        ("incremental", incremental(), "Latest revision"),
        ("cid-tounicode", cid("CID text"), "CID text"),
        ("cjk", cid("漢字中文"), "漢字中文"),
        ("rtl", cid("שלום"), "שלום"),
        ("supplementary", cid("A😀B"), "A😀B"),
        ("rotated-cid", cid("漢字", rotation=90), "漢字"),
    ]
    return [
        Fixture(
            f"pdfium-{name}",
            "pdf",
            "application/pdf",
            data,
            Gold(facts=(value,), regions=(("page", 1, value, None),)),
            case=name,
        )
        for name, data, value in cases
    ]
