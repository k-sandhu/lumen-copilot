"""Generated layout PDFs and gold; source code only, no external documents."""

from __future__ import annotations

from tests.eval.docintel.fixtures import Fixture
from tests.eval.docintel.metrics import Gold


def objects(values: list[bytes], trailer: str = "") -> bytes:
    data = bytearray(b"%PDF-1.7\n")
    offsets = []
    for index, value in enumerate(values, 1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    offset = len(data)
    data.extend(f"xref\n0 {len(values) + 1}\n0000000000 65535 f \n".encode())
    for start in offsets:
        data.extend(f"{start:010} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(values)+1} /Root 1 0 R {trailer} >>\n"
        f"startxref\n{offset}\n%%EOF\n".encode()
    )
    return bytes(data)


def text(x: int, y: int, size: int, value: str) -> bytes:
    escaped = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({escaped}) Tj ET".encode("ascii")


def document(contents: list[bytes], *, rotation: int = 0) -> bytes:
    kids = " ".join(f"{4+i*2} 0 R" for i in range(len(contents)))
    values = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(contents)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
    ]
    for index, content in enumerate(contents):
        values.extend(
            [
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Rotate {rotation} "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5+index*2} 0 R >>".encode(),
                f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
            ]
        )
    return objects(values)


def corpus() -> list[Fixture]:
    from tests.eval.docintel.pdf_table_fixtures import corpus as tables

    content = b"\n".join(
        [
            text(40, 740, 22, "Title"),
            text(40, 680, 12, "LeftOne"),
            text(280, 680, 12, "RightOne"),
            text(470, 680, 10, "Sidebar"),
            text(40, 650, 12, "LeftTwo"),
            text(280, 650, 12, "RightTwo"),
            text(40, 60, 8, "Footnote"),
        ]
    )
    anchors = ("Title", "LeftOne", "LeftTwo", "RightOne", "RightTwo", "Sidebar", "Footnote")
    repeated = b"\n".join(
        [
            text(40, 770, 10, "RepeatedHeader"),
            text(40, 700, 12, "AB-"),
            text(40, 680, 12, "123"),
            text(40, 20, 8, "RepeatedFooter"),
        ]
    )
    return [
        Fixture(
            "pdf-layout-order",
            "pdf",
            "application/pdf",
            document([content]),
            Gold(facts=anchors, order=anchors, regions=(("page", 1, "LeftTwo", None),)),
            case="layout",
        ),
        Fixture(
            "pdf-rotated",
            "pdf",
            "application/pdf",
            document([text(40, 700, 12, "Rotated")], rotation=90),
            Gold(facts=("Rotated",), regions=(("page", 1, "Rotated", None),)),
            case="rotation",
        ),
        Fixture(
            "pdf-furniture-identifiers",
            "pdf",
            "application/pdf",
            document([repeated, repeated]),
            Gold(
                facts=("AB-", "123"), regions=(("page", 1, "AB-", None), ("page", 2, "123", None))
            ),
            case="furniture",
        ),
    ] + tables()
