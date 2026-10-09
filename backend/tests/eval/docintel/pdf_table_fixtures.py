"""Original PDF table and non-table documents; no binary fixtures."""

from __future__ import annotations

from tests.eval.docintel.fixtures import Fixture
from tests.eval.docintel.metrics import Gold
from tests.eval.docintel.pdf_fixtures import document, text


def grid(xs: tuple[int, ...], ys: tuple[int, ...]) -> bytes:
    return b"\n".join(
        [f"{x} {ys[0]} m {x} {ys[-1]} l S".encode() for x in xs]
        + [f"{xs[0]} {y} m {xs[-1]} {y} l S".encode() for y in ys]
    )


def body(y: int, rows: tuple[tuple[str, str], ...]) -> bytes:
    return b"\n".join(
        text(x, y - i * 30, 12, value)
        for i, row in enumerate(rows)
        for x, value in zip((50, 250), row, strict=True)
    )


def corpus() -> list[Fixture]:
    rows = (("Region", "Mass"), ("North", "-120 kg*"), ("South", "+30 kg"))
    gold = Gold(
        facts=("Region", "Mass", "North", "-120", "kg", "South", "+30"),
        associations=(("North", "-120", "kg"), ("South", "+30", "kg")),
        headers=(
            ("Region", "North"),
            ("Mass", "-120 kg*"),
            ("Region", "South"),
            ("Mass", "+30 kg"),
        ),
        regions=(("page", 1, "North", None),),
    )
    content = body(720, rows)
    first = grid((40, 240, 440), (160, 130, 100)) + b"\n" + body(140, rows[:2])
    second = grid((40, 240, 440), (750, 720, 690)) + b"\n" + body(730, (rows[0], rows[2]))
    merged_gold = Gold(
        facts=gold.facts,
        associations=gold.associations,
        headers=gold.headers,
        regions=(("page", 1, "North", None), ("page", 2, "South", None)),
    )
    spanning = (
        grid((40, 440), (740, 710, 680))
        + b"\n240 710 m 240 680 l S\n"
        + text(50, 720, 12, "Shared heading")
        + b"\n"
        + body(690, (("North", "-120 kg*"),))
    )
    fixtures = [
        Fixture(
            "pdf-table-bordered",
            "pdf",
            "application/pdf",
            document([grid((40, 240, 440), (740, 710, 680, 650)) + b"\n" + content]),
            gold,
            case="table-bordered",
        ),
        Fixture(
            "pdf-table-borderless",
            "pdf",
            "application/pdf",
            document([content]),
            gold,
            case="table-borderless",
        ),
        Fixture(
            "pdf-table-multipage",
            "pdf",
            "application/pdf",
            document([first, second]),
            merged_gold,
            case="table-multipage",
        ),
        Fixture(
            "pdf-table-spanned",
            "pdf",
            "application/pdf",
            document([spanning]),
            Gold(
                facts=("Shared heading", "North", "-120", "kg"),
                associations=(("North", "-120", "kg"),),
                headers=(("Shared heading", "North"), ("Shared heading", "-120 kg*")),
                regions=(("page", 1, "North", None),),
            ),
            case="table-spanned",
        ),
    ]
    negatives = [
        (("LeftOne", "RightOne"), ("LeftTwo", "RightTwo"), ("LeftThree", "RightThree")),
        (
            ("Chapter one", "Other chapter"),
            ("Long prose", "More prose"),
            ("Final words", "Closing words"),
        ),
        (
            ("Section 1 overview", "Chapter 2 overview"),
            ("Some narrative text", "Other narrative text"),
            ("Closing narrative", "Closing other text"),
        ),
    ]
    for i, rows in enumerate(negatives):
        fixtures.append(
            Fixture(
                f"pdf-non-table-{i}",
                "pdf",
                "application/pdf",
                document([body(720, rows)]),
                Gold(facts=tuple(value for row in rows for value in row)),
                case="non-table-prose",
            )
        )
    unpainted = (
        grid((40, 240, 440), (740, 710, 680, 650)).replace(b" l S", b" l n")
        + b"\n"
        + body(720, negatives[0])
    )
    fixtures.append(
        Fixture(
            "pdf-non-table-unpainted",
            "pdf",
            "application/pdf",
            document([unpainted]),
            Gold(facts=tuple(value for row in negatives[0] for value in row)),
            case="non-table-prose",
        )
    )
    return fixtures
