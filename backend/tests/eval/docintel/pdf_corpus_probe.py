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
from app.ingestion._pdf_worker import safe_counters
from app.ingestion.native import _pdfium_library

ENGINES = ("pypdf", "in_core", "pdfium")
MAX_BYTES = 100 * 1024 * 1024


def distribution(values: list[int]) -> dict[str, int | None]:
    ordered = sorted(values)
    return {
        key: ordered[min(len(ordered) - 1, (len(ordered) * percent + 99) // 100 - 1)]
        if ordered
        else None
        for key, percent in (("p50", 50), ("p95", 95), ("max", 100))
    }


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


def run(
    directory: Path,
    *,
    workers: int = 2,
    engines: tuple[str, ...] = ENGINES,
    work_units: int = 5_000_000,
) -> dict[str, Any]:
    if (
        not directory.is_dir()
        or not 1 <= workers <= 2
        or not engines
        or len(set(engines)) != len(engines)
        or not set(engines) <= set(ENGINES)
        or not 1 <= work_units <= 20_000_000
    ):
        raise ValueError("invalid corpus probe configuration")
    pool = PdfProcessPool(
        workers=workers, memory_cap_bytes=512 * 1024 * 1024, total_memory_bytes=1024 * 1024 * 1024
    )
    library = _pdfium_library()
    budget = RuntimeBudget(
        max_input_bytes=MAX_BYTES, max_memory_bytes=256 * 1024 * 1024, max_work_units=work_units
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
            "budget_limits": {},
            "ocr_page_reasons": {},
        }
        for engine in engines
    }
    agreement = {
        f"{a}/{b}": {"comparable": 0, "exact_matches": 0, "token_dice_sum": 0.0}
        for a, b in combinations(engines, 2)
    }
    detected = oversized = unreadable = 0
    counters: dict[str, dict[str, list[int]]] = {engine: {} for engine in engines}
    failure_counters: dict[str, dict[str, dict[str, list[int]]]] = {
        engine: {} for engine in engines
    }
    gaps: Counter[str] = Counter()
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
        outcomes: dict[str, str] = {}
        for engine in engines:
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
                for page in (
                    result.get("document", {})
                    .get("generation", {})
                    .get("diagnostics", {})
                    .get("pages", [])
                ):
                    if page.get("outcome") == "needs_ocr":
                        reason = page.get("ocr_reason", "unknown")
                        reasons = total["ocr_page_reasons"]
                        reasons[reason] = reasons.get(reason, 0) + 1
                outcomes[engine] = outcome
                runtime = safe_counters(
                    result.get("document", {})
                    .get("generation", {})
                    .get("diagnostics", {})
                    .get("runtime", {})
                )
                for key, count in runtime.items():
                    if type(count) is int:
                        counters[engine].setdefault(key, []).append(count)
                total["parsed" if outcome == "indexed" else "needs_ocr"] += 1
                total["pages"] += pages
                total["characters"] += len(value)
                total["peak_rss_bytes"] = max(total["peak_rss_bytes"], peak)
                texts[engine] = " ".join(unicodedata.normalize("NFC", value).split())
            except PdfWorkerError as error:
                limit = error.diagnostics.get("limit") or "unattributed"
                outcomes[engine] = f"{error.code}/{limit}" if error.code == "budget" else error.code
                if error.code == "budget":
                    limits = total["budget_limits"]
                    limits[limit] = limits.get(limit, 0) + 1
                by_limit = failure_counters[engine].setdefault(str(limit), {})
                for key, count in error.diagnostics.items():
                    if type(count) is int:
                        by_limit.setdefault(key, []).append(count)
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
                outcomes[engine] = "unexpected_failure"
            total["seconds"] += time.perf_counter() - started
        if outcomes.get("pypdf") == "indexed" and outcomes.get("pdfium") != "indexed":
            gaps[outcomes.get("pdfium", "unknown")] += 1
        for a, b in combinations(engines, 2):
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
        "work_units_max": work_units,
        "python_complete_pdfium_gaps": dict(gaps),
        "counter_distributions": {
            engine: {key: distribution(values) for key, values in samples.items()}
            for engine, samples in counters.items()
        },
        "failure_counter_distributions": {
            engine: {
                limit: {key: distribution(values) for key, values in samples.items()}
                for limit, samples in limits.items()
            }
            for engine, limits in failure_counters.items()
        },
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
    parser.add_argument("--report", type=Path)
    parser.add_argument("--engine", action="append", choices=ENGINES)
    parser.add_argument("--work-units", type=int, default=5_000_000)
    args = parser.parse_args()
    try:
        report = run(
            args.corpus_directory,
            workers=args.workers,
            engines=tuple(args.engine or ENGINES),
            work_units=args.work_units,
        )
    except Exception:
        print("Corpus probe unavailable; no input details emitted.", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(report, indent=2))
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
