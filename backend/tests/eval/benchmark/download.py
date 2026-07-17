"""Download the benchmark corpus and verify (or pin) its checksums (#420).

Usage (from ``backend/``):

    uv run --extra dev python -m tests.eval.benchmark.download            # fetch + verify
    uv run --extra dev python -m tests.eval.benchmark.download --pin      # (re)write pins
    uv run --extra dev python -m tests.eval.benchmark.download --only rfc9112-http11,arxiv-bert
    uv run --extra dev python -m tests.eval.benchmark.download --smoke    # smoke subset only

Idempotent: a file whose bytes already match its pin is not re-fetched. Every
download is streamed to a ``.part`` file, checked against the pinned sha256 and
byte size (plus a cheap magic-byte sanity check per format), and only then moved
into place — a partial or tampered download never lands under the real filename.

``--pin`` is the **maintenance** path: it records the observed sha256/size into
``checksums.json`` for entries that have no pin yet or whose upstream genuinely
changed. Pin diffs are reviewed like code — an unexpected pin change means the
source URL no longer serves the document the questions were authored against.

This is a plain synchronous CLI (test tooling, not app code) — the httpx sync
client is deliberate; nothing here runs on the app's event loop.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from tests.eval.benchmark.manifest import (
    CORPUS,
    Checksum,
    CorpusFile,
    corpus_dir,
    load_checksums,
    manifest_issues,
    save_checksums,
)

# Some hosts (rfc-editor.org among them) reject bare-bot requests or HEAD; a
# browser-family UA that still identifies the project keeps every manifest host
# happy while staying honest about who is fetching.
_USER_AGENT = (
    "Mozilla/5.0 (compatible; lumen-copilot-rag-benchmark/1.0; "
    "+https://github.com/k-sandhu/lumen-copilot)"
)
_TIMEOUT_SECONDS = 120.0
_ATTEMPTS = 3

# Cheap magic-byte sanity checks per declared MIME family: catches an HTML
# error page saved where a PDF/OOXML file should be, before hashing even runs.
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"  # DOCX/PPTX/XLSX are OOXML zip containers


@dataclass(slots=True)
class FetchResult:
    """Outcome of one manifest entry's fetch/verify step."""

    entry: CorpusFile
    status: str  # "ok" | "cached" | "pinned" | "FAILED"
    detail: str
    checksum: Checksum | None = None


def _magic_ok(mime_type: str, head: bytes) -> bool:
    if mime_type == "application/pdf":
        return head.startswith(_PDF_MAGIC)
    if mime_type.startswith("application/vnd.openxmlformats-officedocument."):
        return head.startswith(_ZIP_MAGIC)
    # Text formats (txt/md/csv/html): no reliable magic; reject NUL bytes as a
    # cheap "this is not text" guard.
    return b"\x00" not in head


def _sha256_of(path: Path) -> Checksum:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
            size += len(block)
    return Checksum(sha256=digest.hexdigest(), size_bytes=size)


def _fetch(client: httpx.Client, entry: CorpusFile, dest: Path) -> Checksum:
    """Stream ``entry.url`` to ``dest`` (via a .part temp) and return its checksum."""
    part = dest.with_suffix(dest.suffix + ".part")
    last_error: Exception | None = None
    for _attempt in range(1, _ATTEMPTS + 1):
        try:
            with client.stream("GET", entry.url) as response:
                response.raise_for_status()
                digest = hashlib.sha256()
                size = 0
                with part.open("wb") as handle:
                    for block in response.iter_bytes(chunk_size=1 << 20):
                        digest.update(block)
                        size += len(block)
                        handle.write(block)
            with part.open("rb") as check_handle:
                head = check_handle.read(2048)
            if not _magic_ok(entry.mime_type, head):
                raise ValueError(
                    f"magic-byte check failed for {entry.mime_type} "
                    f"(first bytes: {head[:16]!r}) — likely an HTML error page"
                )
            part.replace(dest)
            return Checksum(sha256=digest.hexdigest(), size_bytes=size)
        except (httpx.HTTPError, ValueError, OSError) as exc:  # retry transient failures
            last_error = exc
            part.unlink(missing_ok=True)
    raise RuntimeError(f"download failed after {_ATTEMPTS} attempts: {last_error}")


def _process_entry(
    client: httpx.Client,
    entry: CorpusFile,
    pins: dict[str, Checksum],
    *,
    dest_dir: Path,
    pin_mode: bool,
    force: bool,
) -> FetchResult:
    dest = dest_dir / entry.filename
    pin = pins.get(entry.file_id)

    # Cached-and-verified short-circuit (the idempotency contract). A rolling
    # entry (#443) short-circuits on ANY cached bytes — its content is allowed
    # to drift; ``force`` (the loader's --refresh) re-fetches it.
    if dest.exists() and not force:
        observed = _sha256_of(dest)
        if entry.rolling:
            return FetchResult(
                entry, "cached", "rolling entry cached (refresh with --force)", observed
            )
        if pin is not None and observed == pin:
            return FetchResult(entry, "cached", "already downloaded, checksum verified", observed)
        if pin_mode:
            pins[entry.file_id] = observed
            return FetchResult(entry, "pinned", "pinned existing local file", observed)
        if pin is None:
            return FetchResult(
                entry, "FAILED", "file present but no pin in checksums.json (run --pin)", observed
            )
        # Local bytes disagree with the pin — re-fetch below.

    try:
        observed = _fetch(client, entry, dest)
    except RuntimeError as exc:
        return FetchResult(entry, "FAILED", str(exc))

    if entry.rolling:
        # Rolling entries always record their last-seen identity; a change from
        # the previous pin is the expected refresh, never a failure.
        changed = pin is not None and observed != pin
        pins[entry.file_id] = observed
        return FetchResult(
            entry,
            "rolled" if changed else ("pinned" if pin_mode or pin is None else "ok"),
            f"rolling entry fetched ({observed.size_bytes:,} B"
            + (", content changed since last seen)" if changed else ")"),
            observed,
        )
    if pin_mode:
        pins[entry.file_id] = observed
        return FetchResult(entry, "pinned", f"downloaded and pinned ({observed.size_bytes:,} B)")
    if pin is None:
        return FetchResult(entry, "FAILED", "no pin in checksums.json (run --pin first)", observed)
    if observed != pin:
        return FetchResult(
            entry,
            "FAILED",
            f"checksum mismatch: expected {pin.sha256[:12]}…/{pin.size_bytes:,} B, "
            f"got {observed.sha256[:12]}…/{observed.size_bytes:,} B — upstream changed; "
            "review and re-pin deliberately",
            observed,
        )
    return FetchResult(entry, "ok", f"downloaded, checksum verified ({observed.size_bytes:,} B)")


def fetch_entries(
    entries: list[CorpusFile],
    pins: dict[str, Checksum],
    *,
    dest_dir: Path,
    pin_mode: bool = False,
    force: bool = False,
) -> list[FetchResult]:
    """Fetch/verify ``entries`` into ``dest_dir`` (the reusable core of ``main``).

    Mutates ``pins`` in ``pin_mode``; the caller decides whether to persist
    them. Also used by the data-pack loader (#441) to ensure its selected
    files are present before uploading.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    results: list[FetchResult] = []
    with httpx.Client(
        follow_redirects=True,
        timeout=_TIMEOUT_SECONDS,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        for entry in entries:
            result = _process_entry(
                client, entry, pins, dest_dir=dest_dir, pin_mode=pin_mode, force=force
            )
            results.append(result)
            print(f"[{result.status:>6}] {entry.file_id}: {result.detail}")
    return results


def _select(only: str | None, smoke: bool) -> list[CorpusFile]:
    entries = list(CORPUS)
    if smoke:
        entries = [e for e in entries if e.smoke]
    if only:
        wanted = {token.strip() for token in only.split(",") if token.strip()}
        known = {e.file_id for e in CORPUS}
        unknown = wanted - known
        if unknown:
            raise SystemExit(f"unknown --only file ids: {', '.join(sorted(unknown))}")
        entries = [e for e in entries if e.file_id in wanted]
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download + verify the benchmark corpus.")
    parser.add_argument("--pin", action="store_true", help="record observed checksums as pins")
    parser.add_argument("--force", action="store_true", help="re-download even if cached")
    parser.add_argument("--smoke", action="store_true", help="only the smoke subset")
    parser.add_argument("--only", help="comma-separated file_ids to fetch")
    parser.add_argument("--dest", help="override the corpus directory")
    args = parser.parse_args(argv)

    problems = manifest_issues()
    if problems:
        for issue in problems:
            print(f"MANIFEST {issue.file_id}: {issue.problem}", file=sys.stderr)
        return 2

    dest_dir = Path(args.dest) if args.dest else corpus_dir()
    pins = load_checksums()
    entries = _select(args.only, args.smoke)
    results = fetch_entries(entries, pins, dest_dir=dest_dir, pin_mode=args.pin, force=args.force)

    if args.pin:
        save_checksums(pins)
        print(f"\npinned {len(pins)} checksums -> checksums.json")

    failed = [r for r in results if r.status == "FAILED"]
    print(
        f"\n{len(results)} entries: "
        f"{sum(1 for r in results if r.status in ('ok', 'cached'))} verified, "
        f"{sum(1 for r in results if r.status == 'pinned')} pinned, {len(failed)} failed"
    )
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
