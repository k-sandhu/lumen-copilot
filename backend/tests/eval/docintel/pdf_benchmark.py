"""PDF arms for the #670 subprocess benchmark; no promotion policy."""

from __future__ import annotations

import base64
import io
import json
import time
from dataclasses import asdict
from typing import Any

from tests.eval.docintel.metrics import Gold, evaluate


def measure(payload: dict[str, Any]) -> dict[str, Any]:
    from tests.eval.docintel.benchmark import _rss

    before = _rss()
    started = time.perf_counter()
    text = ""
    spans = ()
    parts = ()
    headers = ()
    extraction_outcome = "failed"
    outcome = "failed"
    code = None
    accounted = None
    tables = 0
    boxes = 0
    try:
        data = base64.b64decode(payload["data"], validate=True)
        if len(data) > 32 * 1024 * 1024:
            raise ValueError("input budget")
        if payload["arm"] == "python-provenance-baseline":
            # Equivalent PDF-only projection to #625: unchanged text and blank parts.
            from pypdf import PdfReader

            pages = [page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages]
            offset = 0
            source_parts = []
            for number, page_text in enumerate(pages, 1):
                if number > 1:
                    offset += 2
                source_parts.append(
                    ("page", number, f"Page {number}", offset, offset + len(page_text))
                )
                offset += len(page_text)
            parts = tuple(source_parts)
            text = "\n\n".join(pages)
            spans = tuple(
                (p[3], p[4], page_text) for p, page_text in zip(parts, pages, strict=True)
            )
            outcome = extraction_outcome = "indexed" if text.strip() else "empty"
        else:
            from app.ingestion.native import NativeExecutor

            document = NativeExecutor(threads=payload.get("threads") or 2).extract_pdf(data)
            text = document.rendered_text
            generation = json.loads(document.generation_json)
            extraction_outcome = generation["outcome"]
            outcome = "indexed" if extraction_outcome == "indexed" else "failed"
            code = None if outcome == "indexed" else "needs_ocr"
            spans = tuple(
                (s.char_start, s.char_end, b.text)
                for s, b in zip(document.spans, document.blocks, strict=True)
            )
            parts = tuple(
                (p.kind, p.number, p.name, p.char_start, p.char_end) for p in document.source_parts
            )
            tables = sum(b.table is not None for b in document.blocks)
            boxes = sum(r.bbox is not None for b in document.blocks for r in b.regions)
            pairs = []
            for block in document.blocks:
                if block.table:
                    by_column = {
                        column: c.text
                        for c in block.table.cells
                        if c.header_role in {"column", "both"}
                        for column in range(c.column, c.column + c.column_span)
                    }
                    pairs.extend(
                        (by_column[c.column], c.text)
                        for c in block.table.cells
                        if c.header_role == "unknown" and c.column in by_column
                    )
            headers = tuple(pairs)
            accounted = generation["diagnostics"]["runtime"]["peak_accounted_bytes"]
    except Exception as error:
        code = type(error).__name__
        outcome = extraction_outcome = "failed"
    elapsed = time.perf_counter() - started
    gold = Gold(
        **{
            k: tuple(tuple(x) if isinstance(x, list) else x for x in v)
            if isinstance(v, list)
            else v
            for k, v in payload["gold"].items()
        }
    )
    peak = _rss()
    return {
        "arm": payload["arm"],
        "outcome": outcome,
        "extraction_outcome": extraction_outcome,
        "code": code,
        "seconds": elapsed,
        "threads": payload.get("threads") or 2,
        "peak_rss_bytes": peak,
        "peak_rss_increment_bytes": max(0, peak - before),
        "peak_accounted_bytes": accounted,
        "table_count": tables,
        "box_count": boxes,
        "input_bytes_per_second": len(data) / elapsed if elapsed else None,
        "output_chars_per_second": len(text) / elapsed if elapsed else None,
        "score": asdict(
            evaluate(
                text, gold, spans=spans, source_parts=parts, cell_headers=headers, outcome=outcome
            )
        ),
    }
