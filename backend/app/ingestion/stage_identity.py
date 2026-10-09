"""Local extraction identities for resumable processing; no network or paths in output."""

from __future__ import annotations

import hashlib
import sys
from importlib.metadata import version
from importlib.resources import files

_LIBRARIES = {
    "application/pdf": ("pypdf",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
        "python-docx",
        "lxml",
    ),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
        "python-pptx",
        "lxml",
    ),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ("openpyxl",),
}


def parser_identity(mime_type: str) -> dict[str, object]:
    def build_hash(name: str) -> str:
        content = files("app.ingestion").joinpath(name).read_bytes().replace(b"\r\n", b"\n")
        return hashlib.sha256(content).hexdigest()

    normalized = mime_type.split(";", 1)[0].strip().lower()
    return {
        "parser_sha256": build_hash("parsers.py"),
        "identity_sha256": build_hash("stage_identity.py"),
        "python": list(sys.version_info[:3]),
        "implementation": sys.implementation.name,
        "libraries": {name: version(name) for name in _LIBRARIES.get(normalized, ())},
    }
