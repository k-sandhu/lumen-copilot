"""Load the benchmark data pack into YOUR profile — deterministic, selectable (#441).

Seeds the pinned real-world corpus into any signed-in user's account on a
running stack: pick the formats and how many files, and the same flags always
produce the same set. Useful for demos, QA sessions, and dev profiles that want
realistic documents without running the eval.

Usage (from ``backend/``; against the primary dev stack by default):

    uv run --extra dev python -m tests.eval.benchmark.load_pack \
        --api http://localhost:47181 --email you@acme.test --password ... \
        --formats pdf,docx,xlsx --count 10

    # preview the exact deterministic selection without touching anything:
    uv run --extra dev python -m tests.eval.benchmark.load_pack \
        --formats pdf,xlsx --count 5 --dry-run

**Deterministic selection.** Eligible files are the manifest's ingestable
entries (never the deliberate negative-format files). The chosen formats are
cycled **round-robin in the order you gave them** (defaults to the canonical
``txt, md, pdf, docx, pptx, xlsx``), taking each format's files in manifest
order — so any ``--count`` yields balanced format coverage and the same flags
always select the same files. Missing files are downloaded first (checksum
pins verified); uploads are idempotent (files already in your profile are
skipped by filename), so re-running is a no-op that reports what's there.

Credentials can come from flags or the ``LUMEN_PACK_EMAIL`` /
``LUMEN_PACK_PASSWORD`` environment variables. Sync httpx is deliberate —
host-side tooling, not app code.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import deque
from collections.abc import Sequence

import httpx

from tests.eval.benchmark.client import ApiClient
from tests.eval.benchmark.download import fetch_entries
from tests.eval.benchmark.manifest import (
    CORPUS,
    CorpusFile,
    corpus_dir,
    load_checksums,
    size_class,
)
from tests.eval.benchmark.packs import PACKS, pack_by_id, pack_files

_INGEST_TIMEOUT_SECONDS = 45 * 60

# Friendly format names -> upload MIME types, in the canonical round-robin
# order used when --formats is omitted.
FORMAT_MIME: dict[str, str] = {
    "txt": "text/plain",
    "md": "text/markdown",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def select_pack(
    formats: Sequence[str] | None = None,
    count: int | None = None,
    *,
    corpus: tuple[CorpusFile, ...] = CORPUS,
) -> tuple[CorpusFile, ...]:
    """The deterministic selection rule (see module docstring).

    ``formats`` are friendly names (case-insensitive, order-significant,
    duplicates collapsed); ``None`` means all six in canonical order.
    ``count=None`` means every eligible file. Raises ``ValueError`` on unknown
    formats or a non-positive count — never a silent empty selection.
    """
    if formats is None:
        chosen = list(FORMAT_MIME)
    else:
        chosen = []
        for name in formats:
            key = name.strip().lower()
            if not key:
                continue  # blank tokens (e.g. a trailing comma) are not formats
            if key not in FORMAT_MIME:
                raise ValueError(f"unknown format {name!r} (choose from {', '.join(FORMAT_MIME)})")
            if key not in chosen:
                chosen.append(key)
        if not chosen:
            raise ValueError("at least one format is required")
    if count is not None and count <= 0:
        raise ValueError("count must be a positive integer")

    queues: dict[str, deque[CorpusFile]] = {
        key: deque(
            e for e in corpus if e.expected_ingest == "ok" and e.mime_type == FORMAT_MIME[key]
        )
        for key in chosen
    }
    selected: list[CorpusFile] = []
    limit = count if count is not None else sum(len(q) for q in queues.values())
    while len(selected) < limit and any(queues.values()):
        for key in chosen:
            if queues[key]:
                selected.append(queues[key].popleft())
                if len(selected) >= limit:
                    break
    return tuple(selected)


def select_from_pack(
    pack_id: str,
    formats: Sequence[str] | None = None,
    count: int | None = None,
) -> tuple[CorpusFile, ...]:
    """Deterministic pack selection: curated pack order, optionally filtered.

    ``--formats`` narrows to those formats (pack order preserved); ``--count``
    takes the first N of what remains. No round-robin here — a pack's order is
    itself the curation.
    """
    files = pack_files(pack_by_id(pack_id))
    if formats is not None:
        keys = []
        for name in formats:
            key = name.strip().lower()
            if not key:
                continue
            if key not in FORMAT_MIME:
                raise ValueError(f"unknown format {name!r} (choose from {', '.join(FORMAT_MIME)})")
            keys.append(key)
        if not keys:
            raise ValueError("at least one format is required")
        mimes = {FORMAT_MIME[k] for k in keys}
        files = tuple(e for e in files if e.mime_type in mimes)
    if count is not None:
        if count <= 0:
            raise ValueError("count must be a positive integer")
        files = files[:count]
    return files


def _print_selection(selection: tuple[CorpusFile, ...]) -> None:
    pins = load_checksums()
    print(f"{len(selection)} file(s) selected (deterministic):")
    for entry in selection:
        pin = pins.get(entry.file_id)
        size = f"{pin.size_bytes:,} B ({size_class(pin.size_bytes)})" if pin else "unpinned"
        fmt = next(k for k, v in FORMAT_MIME.items() if v == entry.mime_type)
        marker = "  (rolling)" if entry.rolling else ""
        print(f"  {fmt:>4}  {entry.file_id:<34} {size}{marker}")


def _print_packs() -> None:
    print("Available industry packs:")
    for pack in PACKS:
        rolling = sum(1 for e in pack_files(pack) if e.rolling)
        print(f"\n  {pack.pack_id}  —  {pack.name} ({pack.industry})")
        print(
            f"      {len(pack.file_ids)} files"
            + (f", {rolling} rolling (refresh with --refresh)" if rolling else "")
        )
        print(f"      {pack.rationale}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load the benchmark data pack into a profile.")
    parser.add_argument("--api", default="http://localhost:47181", help="stack base URL")
    parser.add_argument("--email", default=os.environ.get("LUMEN_PACK_EMAIL", ""))
    parser.add_argument("--password", default=os.environ.get("LUMEN_PACK_PASSWORD", ""))
    parser.add_argument("--collection", default="RAG benchmark pack")
    parser.add_argument("--pack", help="industry pack id (see --list-packs)")
    parser.add_argument("--list-packs", action="store_true", help="show the pack catalog")
    parser.add_argument("--formats", help="comma-separated: txt,md,pdf,docx,pptx,xlsx")
    parser.add_argument("--count", type=int, help="max files (default: all matching)")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-download rolling entries and replace them in the profile",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the selection and exit")
    args = parser.parse_args(argv)

    if args.list_packs:
        _print_packs()
        return 0

    formats = [t for t in args.formats.split(",") if t.strip()] if args.formats else None
    try:
        if args.pack:
            selection = select_from_pack(args.pack, formats, args.count)
        else:
            selection = select_pack(formats, args.count)
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_selection(selection)
    if args.dry_run:
        return 0
    if not args.email or not args.password:
        print(
            "error: --email/--password (or LUMEN_PACK_EMAIL/LUMEN_PACK_PASSWORD) required",
            file=sys.stderr,
        )
        return 2

    # Ensure the selected files are on disk (checksum-verified against the pins);
    # with --refresh, rolling entries re-fetch even when cached.
    directory = corpus_dir()
    missing = [e for e in selection if not (directory / e.filename).exists()]
    to_refresh = [
        e for e in selection if e.rolling and args.refresh and (directory / e.filename).exists()
    ]
    if missing or to_refresh:
        print(f"\ndownloading {len(missing)} missing, refreshing {len(to_refresh)} rolling…")
        results = fetch_entries(missing, load_checksums(), dest_dir=directory)
        results += fetch_entries(to_refresh, load_checksums(), dest_dir=directory, force=True)
        failed = [r for r in results if r.status == "FAILED"]
        if failed:
            print(f"error: {len(failed)} download(s) failed; aborting", file=sys.stderr)
            return 1

    with httpx.Client(timeout=httpx.Timeout(60.0, read=300.0)) as http:
        api = ApiClient(http, args.api, args.email, args.password)
        api.login()
        collection_id = api.ensure_collection(args.collection)
        print(f"\ncollection '{args.collection}': {collection_id}")

        existing = api.existing_documents()
        uploaded: set[str] = set()
        replaced = 0
        for entry in selection:
            if entry.filename in existing:
                if entry.rolling and args.refresh:
                    # Rolling refresh: replace the profile's copy with the
                    # freshly-fetched content (delete, then re-upload below).
                    api.delete_document(str(existing[entry.filename]["id"]))
                    replaced += 1
                    print(f"[replac] {entry.file_id}: refreshing rolling document")
                else:
                    print(
                        f"[  skip] {entry.file_id}: already in profile "
                        f"(status={existing[entry.filename]['status']})"
                    )
                    continue
            payload = (directory / entry.filename).read_bytes()
            response = api.upload_document(
                collection_id=collection_id,
                filename=entry.filename,
                data=payload,
                mime_type=entry.mime_type,
            )
            if response.status_code != 201:
                print(
                    f"[FAILED] {entry.file_id}: {response.status_code} {response.text[:120]}",
                    file=sys.stderr,
                )
                return 1
            uploaded.add(entry.filename)
            print(f"[upload] {entry.file_id}")

        if uploaded:
            print(f"\nwaiting for ingestion of {len(uploaded)} file(s)…")
            outcomes = api.wait_for_documents(uploaded, timeout_seconds=_INGEST_TIMEOUT_SECONDS)
            not_ready = {f: s for f, s in outcomes.items() if s != "ready"}
            if not_ready:
                print(f"error: not ready: {not_ready}", file=sys.stderr)
                return 1
        print(
            f"\ndone — {len(selection)} file(s) in your profile "
            f"({len(uploaded) - replaced} new, {replaced} refreshed)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
