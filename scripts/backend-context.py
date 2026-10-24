"""Read-only backend context preflight; never follows links or removes files."""

import argparse
import os
from pathlib import Path
import re
import stat

DRIVE = re.compile(r"^[A-Za-z][:\uf03a]$")


def inspect(root: Path) -> list[tuple[str, str]]:
    findings = []
    pending = [root.absolute()]
    while pending:
        directory = pending.pop()
        try:
            metadata = directory.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                findings.append((str(directory), "link/reparse point (not followed)"))
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    metadata = entry.stat(follow_symlinks=False)
                    if (
                        entry.is_symlink()
                        or getattr(metadata, "st_file_attributes", 0)
                        & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    ):
                        findings.append(
                            (str(path), "link/reparse point (not followed)")
                        )
                    elif DRIVE.fullmatch(entry.name):
                        findings.append(
                            (str(path), "Windows drive tree (not traversed)")
                        )
                    elif entry.name == "pyvenv.cfg":
                        findings.append(
                            (str(directory), "local/foreign virtualenv (pyvenv.cfg)")
                        )
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(path)
        except OSError as error:
            findings.append((str(directory), f"unreadable path: {error}"))
    return sorted(set(findings))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend", type=Path, default=Path(__file__).resolve().parents[1] / "backend"
    )
    args = parser.parse_args()
    findings = inspect(args.backend)
    for path, reason in findings:
        print(f"{path!r}: {reason}")
    if findings:
        print(
            "No files changed. Inspect each exact path above. Close tools using it, then move that exact artifact to a quarantine directory OUTSIDE the checkout using your file manager. Do not recursively delete a computed path. Re-run this preflight. See docs/runbooks/backend-build-context.md."
        )
    else:
        print(
            "Backend context preflight: no drive trees, virtualenvs, links, or unreadable paths found."
        )
    return bool(findings)


if __name__ == "__main__":
    raise SystemExit(main())
