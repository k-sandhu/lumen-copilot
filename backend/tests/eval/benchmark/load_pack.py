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

**Packs.** ``--pack <id>`` loads a curated set in its own order instead of the
round-robin selection (``--list-packs`` shows the catalog). Tax-research packs
additionally declare which aspect of filing each file answers for, so
``--tax-topic <topic>`` narrows a pack to just that slice — e.g. only the
payroll-withholding documents. Filters compose; if they leave nothing to load
that is an error, not an empty run.

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
from tests.eval.benchmark.packs import (
    PACKS,
    TAX_FAMILY,
    TAX_TOPIC_LABELS,
    pack_by_id,
    pack_files,
    pack_files_for_topic,
    topics_of,
)

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
    tax_topic: str | None = None,
) -> tuple[CorpusFile, ...]:
    """Deterministic pack selection: curated pack order, optionally filtered.

    ``tax_topic`` narrows a tax-research pack to the files that cover one aspect
    of filing (``--tax-topic payroll_withholding`` for just the payroll set);
    ``--formats`` narrows to those formats; ``--count`` takes the first N of what
    remains. Pack order is preserved throughout — no round-robin, because a
    pack's order is itself the curation.

    Raises ``ValueError`` on a bad format/count or when the filters leave nothing
    to load, and ``KeyError`` on an unknown pack or topic — never a silent empty
    selection.
    """
    pack = pack_by_id(pack_id)
    files = pack_files_for_topic(pack, tax_topic) if tax_topic else pack_files(pack)
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
    if not files:
        raise ValueError(
            f"no files in pack {pack_id!r} match "
            + (f"topic {tax_topic!r} and " if tax_topic else "")
            + f"formats {','.join(formats) if formats else 'any'}"
        )
    return files


def _print_selection(selection: tuple[CorpusFile, ...], pack_id: str | None = None) -> None:
    pins = load_checksums()
    pack = pack_by_id(pack_id) if pack_id else None
    print(f"{len(selection)} file(s) selected (deterministic):")
    for entry in selection:
        pin = pins.get(entry.file_id)
        size = f"{pin.size_bytes:,} B ({size_class(pin.size_bytes)})" if pin else "unpinned"
        fmt = next(k for k, v in FORMAT_MIME.items() if v == entry.mime_type)
        marker = "  (rolling)" if entry.rolling else ""
        topics = ", ".join(topics_of(pack, entry.file_id)) if pack else ""
        suffix = f"{marker}  {topics}" if topics else marker
        print(f"  {fmt:>4}  {entry.file_id:<42} {size}{suffix}")


def _print_packs() -> None:
    print("Available packs:")
    for pack in PACKS:
        rolling = sum(1 for e in pack_files(pack) if e.rolling)
        family = "" if pack.family == "industry" else f"  [{pack.family}]"
        print(f"\n  {pack.pack_id}  —  {pack.name} ({pack.industry}){family}")
        print(
            f"      {len(pack.file_ids)} files"
            + (f", {rolling} rolling (refresh with --refresh)" if rolling else "")
        )
        print(f"      {pack.rationale}")
        if pack.family == TAX_FAMILY:
            # A tax pack's promise is coverage, so show it: every aspect of
            # filing and how many of the pack's files answer for it.
            print("      tax topics (--tax-topic <id>):")
            for coverage in pack.tax_coverage:
                label = TAX_TOPIC_LABELS[coverage.topic]
                print(f"        {coverage.topic:<20} {len(coverage.file_ids):>2} file(s)  {label}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load the benchmark data pack into a profile.")
    parser.add_argument("--api", default="http://localhost:47181", help="stack base URL")
    parser.add_argument("--email", default=os.environ.get("LUMEN_PACK_EMAIL", ""))
    parser.add_argument("--password", default=os.environ.get("LUMEN_PACK_PASSWORD", ""))
    parser.add_argument("--collection", default="RAG benchmark pack")
    parser.add_argument("--pack", help="pack id (see --list-packs)")
    parser.add_argument("--list-packs", action="store_true", help="show the pack catalog")
    parser.add_argument(
        "--tax-topic",
        help="narrow a tax-research pack to one aspect of filing, e.g. "
        f"{', '.join(list(TAX_TOPIC_LABELS)[:3])}, … (requires --pack)",
    )
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

    if args.tax_topic and not args.pack:
        print("error: --tax-topic requires --pack", file=sys.stderr)
        return 2

    formats = [t for t in args.formats.split(",") if t.strip()] if args.formats else None
    try:
        if args.pack:
            selection = select_from_pack(args.pack, formats, args.count, args.tax_topic)
        else:
            selection = select_pack(formats, args.count)
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_selection(selection, args.pack)
    if args.dry_run:
        return 0
    if not args.email or not args.password:
        print(
            "error: --email/--password (or LUMEN_PACK_EMAIL/LUMEN_PACK_PASSWORD) required",
            file=sys.stderr,
        )
        return 2

    # Put EVERY selected file through fetch_entries, not just the missing ones.
    # A cached file is only trustworthy if its bytes still match the pin, and
    # fetch_entries is the single place that check lives — skipping it for
    # anything already on disk is exactly how a previously-failed download could
    # be uploaded as if it were corpus content.
    directory = corpus_dir()
    to_refresh = [
        e for e in selection if e.rolling and args.refresh and (directory / e.filename).exists()
    ]
    verify_first = [e for e in selection if e not in to_refresh]
    print(f"\nverifying {len(verify_first)} file(s), refreshing {len(to_refresh)} rolling…")
    results = fetch_entries(verify_first, load_checksums(), dest_dir=directory)
    results += fetch_entries(to_refresh, load_checksums(), dest_dir=directory, force=True)
    failed = [r for r in results if r.status == "FAILED"]
    if failed:
        print(
            f"error: {len(failed)} file(s) failed download/verification; aborting "
            f"({', '.join(r.entry.file_id for r in failed)})",
            file=sys.stderr,
        )
        return 1

    with httpx.Client(timeout=httpx.Timeout(60.0, read=300.0)) as http:
        api = ApiClient(http, args.api, args.email, args.password)
        api.login()
        collection_id = api.ensure_collection(args.collection)
        print(f"\ncollection '{args.collection}': {collection_id}")

        # Scoped to the target collection: a profile-wide map keyed by filename
        # would let a same-named document in ANOTHER collection be mistaken for
        # ours — skipped as "already there", or deleted during a refresh.
        existing = api.existing_documents(collection_id=collection_id)
        uploaded: set[str] = set()
        replaced = 0
        # Old rolling copies to retire, retired only AFTER their replacement is
        # ingested and ready. Deleting first — as this used to — meant a failed
        # upload or ingestion left the profile with nothing: old copy gone, no
        # new one. The worst case now is a transient duplicate, which a re-run
        # reconciles; the previous worst case was silent data loss.
        supersede: dict[str, str] = {}
        for entry in selection:
            if entry.filename in existing:
                if entry.rolling and args.refresh:
                    supersede[entry.filename] = str(existing[entry.filename]["id"])
                    replaced += 1
                    print(f"[replac] {entry.file_id}: uploading refreshed copy first")
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
                # Leave every superseded document in place: the refreshed copies
                # are not usable, so deleting the old ones now would destroy the
                # only working content the profile has.
                print(f"error: not ready: {not_ready}", file=sys.stderr)
                if supersede:
                    print(
                        f"note: {len(supersede)} superseded document(s) left in place "
                        "(their replacements are not ready) — re-run to reconcile",
                        file=sys.stderr,
                    )
                return 1

        # Only now, with every replacement ingested and ready, retire the copies
        # they supersede.
        for filename, document_id in supersede.items():
            api.delete_document(document_id)
            print(f"[retire] {filename}: previous copy removed")

        print(
            f"\ndone — {len(selection)} file(s) in your profile "
            f"({len(uploaded) - replaced} new, {replaced} refreshed)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
