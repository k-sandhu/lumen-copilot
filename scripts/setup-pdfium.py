"""Install only a SHA-pinned non-V8 PDFium release; retain all licence notices."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def host_platform() -> str:
    system = {"Windows": "win", "Linux": "linux", "Darwin": "mac"}[platform.system()]
    arch = {"AMD64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}[
        platform.machine()
    ]
    return f"{system}-{arch}"


def install(target: str, destination: Path) -> None:
    manifest = json.loads(
        (ROOT / "rust/pdfium-binaries.json").read_text(encoding="utf-8")
    )
    pin = manifest["platforms"][target]
    url = (
        "https://github.com/bblanchon/pdfium-binaries/releases/download/"
        f"{manifest['release']}/pdfium-{target}.tgz"
    )
    with tempfile.TemporaryDirectory() as scratch:
        archive = Path(scratch) / "pdfium.tgz"
        digest = hashlib.sha256()
        with (
            urllib.request.urlopen(url, timeout=60) as response,
            archive.open("wb") as output,
        ):
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 100 * 1024 * 1024:
                    raise ValueError("PDFium archive exceeds build limit")
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != pin["sha256"]:
            raise ValueError("PDFium archive checksum mismatch")
        extracted = Path(scratch) / "extracted"
        with tarfile.open(archive) as source:
            source.extractall(extracted, filter="data")
        if not (extracted / pin["library"]).is_file():
            raise ValueError("PDFium library missing from verified archive")
        destination.mkdir(parents=True, exist_ok=True)
        # Keep the complete upstream archive topology, including licenses/.
        shutil.copytree(extracted, destination, dirs_exist_ok=True)
        (destination / "pin.json").write_text(
            json.dumps({"release": manifest["release"], "platform": target, **pin})
            + "\n",
            encoding="utf-8",
        )
    print(f"Verified PDFium {manifest['release']} for {target}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default=host_platform())
    parser.add_argument(
        "--destination",
        type=Path,
        default=ROOT / "rust/crates/lumen-docintel-py/pdfium",
    )
    args = parser.parse_args()
    install(args.platform, args.destination)
