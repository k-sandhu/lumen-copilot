"""Extract plain text from the downloaded corpus with the REAL ingestion parsers (#420).

The question bank's evidence quotes are verbatim substrings of what
:func:`app.ingestion.parsers.parse_document` extracts — not of the source
bytes — so groundedness checks measure exactly what the pipeline can retrieve.
This module is the single place that extraction happens for the benchmark;
results are cached under ``corpus/_extracted/<file_id>.txt`` so authoring,
verification, and the golden-set adapter all read the same text.

Usage (from ``backend/``):

    uv run --extra dev python -m tests.eval.benchmark.extract           # all ingestable
    uv run --extra dev python -m tests.eval.benchmark.extract --force   # re-extract
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.ingestion.parsers import parse_document
from tests.eval.benchmark.manifest import (
    CORPUS,
    CorpusFile,
    corpus_dir,
    entry_by_id,
    extracted_dir,
)


def extract_text(entry: CorpusFile, *, base_dir: Path | None = None) -> str:
    """Parse ``entry``'s downloaded bytes to plain text via the real parsers.

    Raises the parser's typed errors untouched (``UnsupportedMimeTypeError`` for
    the deliberate negative entries — callers assert on that) and
    ``FileNotFoundError`` if the corpus file has not been downloaded.
    """
    directory = base_dir if base_dir is not None else corpus_dir()
    data = (directory / entry.filename).read_bytes()
    return parse_document(data, mime_type=entry.mime_type)


def cached_text(file_id: str) -> str:
    """Read the cached extraction for ``file_id`` (extract on miss)."""
    entry = entry_by_id(file_id)
    cache_path = extracted_dir() / f"{entry.file_id}.txt"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    text = extract_text(entry)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(text, encoding="utf-8")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract corpus text via the real parsers.")
    parser.add_argument("--force", action="store_true", help="re-extract over the cache")
    parser.add_argument("--only", help="comma-separated file_ids")
    args = parser.parse_args(argv)

    wanted: set[str] | None = None
    if args.only:
        wanted = {token.strip() for token in args.only.split(",") if token.strip()}

    out_dir = extracted_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for entry in CORPUS:
        if wanted is not None and entry.file_id not in wanted:
            continue
        if entry.expected_ingest != "ok":
            print(f"[  skip] {entry.file_id}: expected_ingest={entry.expected_ingest}")
            continue
        cache_path = out_dir / f"{entry.file_id}.txt"
        if cache_path.exists() and not args.force:
            print(f"[cached] {entry.file_id}: {cache_path.stat().st_size:,} chars on disk")
            continue
        try:
            text = extract_text(entry)
        except FileNotFoundError:
            print(f"[FAILED] {entry.file_id}: not downloaded (run …benchmark.download)")
            failures += 1
            continue
        except Exception as exc:  # noqa: BLE001 — CLI reporting surface
            print(f"[FAILED] {entry.file_id}: {type(exc).__name__}: {exc}")
            failures += 1
            continue
        cache_path.write_text(text, encoding="utf-8")
        print(f"[    ok] {entry.file_id}: {len(text):,} chars extracted")
    if failures:
        print(f"\n{failures} extraction(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
