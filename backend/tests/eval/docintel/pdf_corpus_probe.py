"""Opt-in local-only PDF probe. Output contains aggregates and no input identities."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any

from app.domain.native_runtime import RuntimeBudget
from app.ingestion._pdf_pool import PdfProcessPool, PdfWorkerError
from app.ingestion.native import _pdfium_library

ENGINES = ("pypdf", "in_core", "pdfium")
MAX_BYTES = 100 * 1024 * 1024


def read_pdf(path: Path) -> bytes | None:
    """Stream bounded windows; never load the whole corpus or serialize a path."""
    with path.open("rb") as source:
        header = source.read(5)
        if header != b"%PDF-":
            return None
        if path.stat().st_size > MAX_BYTES:
            raise OverflowError
        value = bytearray(header)
        while chunk := source.read(65536):
            if len(value) + len(chunk) > MAX_BYTES:
                raise OverflowError
            value.extend(chunk)
        return bytes(value)


def run(directory: Path, *, workers: int = 2) -> dict[str, Any]:
    if not directory.is_dir() or not 1 <= workers <= 2:
        raise ValueError("invalid corpus probe configuration")
    pool = PdfProcessPool(
        workers=workers, memory_cap_bytes=512 * 1024 * 1024, total_memory_bytes=1024 * 1024 * 1024
    )
    library = _pdfium_library()
    budget = RuntimeBudget(
        max_input_bytes=MAX_BYTES, max_memory_bytes=256 * 1024 * 1024, max_work_units=5_000_000
    )
    totals = {
        engine: {
            "parsed": 0,
            "needs_ocr": 0,
            "unsupported": 0,
            "failed": 0,
            "timed_out": 0,
            "pages": 0,
            "characters": 0,
            "seconds": 0.0,
            "peak_rss_bytes": 0,
            "error_codes": {},
        }
        for engine in ENGINES
    }
    agreement = {
        f"{a}/{b}": {"comparable": 0, "exact_matches": 0, "token_dice_sum": 0.0}
        for a, b in combinations(ENGINES, 2)
    }
    detected = oversized = unreadable = 0
    # Only aggregates survive each iteration. All three engines use OS-supervised
    # children and identical budgets; do not print exceptions or subprocess stderr.
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        try:
            data = read_pdf(path)
        except OverflowError:
            oversized += 1
            continue
        except OSError:
            unreadable += 1
            continue
        if data is None:
            continue
        detected += 1
        texts: dict[str, str] = {}
        for engine in ENGINES:
            started = time.perf_counter()
            total = totals[engine]
            try:
                encoded, peak = pool.extract(data, library=library, engine=engine, budget=budget)
                result = json.loads(encoded)
                if engine == "pypdf":
                    pages = result["pages"]
                    outcome = result["outcome"]
                else:
                    pages = len(result["document"]["source_parts"])
                    outcome = result["document"]["generation"]["outcome"]
                value = result["rendered_text"]
                total["parsed" if outcome == "indexed" else "needs_ocr"] += 1
                total["pages"] += pages
                total["characters"] += len(value)
                total["peak_rss_bytes"] = max(total["peak_rss_bytes"], peak)
                texts[engine] = " ".join(unicodedata.normalize("NFC", value).split())
            except PdfWorkerError as error:
                errors = total["error_codes"]
                errors[error.code] = errors.get(error.code, 0) + 1
                category = (
                    "timed_out"
                    if error.code == "timed_out"
                    else "unsupported"
                    if error.code in {"unsupported", "encrypted"}
                    else "failed"
                )
                total[category] += 1
                total["peak_rss_bytes"] = max(total["peak_rss_bytes"], error.peak_rss_bytes)
            except Exception:
                total["failed"] += 1
            total["seconds"] += time.perf_counter() - started
        for a, b in combinations(ENGINES, 2):
            if a not in texts or b not in texts:
                continue
            score = agreement[f"{a}/{b}"]
            score["comparable"] += 1
            score["exact_matches"] += int(texts[a] == texts[b])
            left = Counter(re.findall(r"\w+|[^\w\s]", texts[a]))
            right = Counter(re.findall(r"\w+|[^\w\s]", texts[b]))
            size = sum(left.values()) + sum(right.values())
            score["token_dice_sum"] += 2 * sum((left & right).values()) / size if size else 1.0
        del data, texts
    for score in agreement.values():
        score["exact_agreement"] = (
            score["exact_matches"] / score["comparable"] if score["comparable"] else None
        )
        score["token_dice_mean"] = (
            score.pop("token_dice_sum") / score["comparable"] if score["comparable"] else None
        )
        score["exact_all_document_rate"] = score["exact_matches"] / detected if detected else None
    return {
        "pdf_documents": detected,
        "skipped_over_100_mib": oversized,
        "unreadable_entries": unreadable,
        "engines": totals,
        "agreement": agreement,
        "workers_max": workers,
        "method": (
            "cold recycled workers; pages/chars include partial OCR outcomes; "
            "time includes startup and IPC; exact agreement normalizes NFC/whitespace; "
            "token Dice is multiset overlap, not fidelity"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-directory", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2, choices=(1, 2))
    args = parser.parse_args()
    try:
        report = run(args.corpus_directory, workers=args.workers)
    except Exception:
        print("Corpus probe unavailable; no input details emitted.", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
