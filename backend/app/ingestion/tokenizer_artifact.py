"""Bounded local tokenizer loading; no downloads or model-name guesses."""

from __future__ import annotations

import hashlib
from pathlib import Path


def load_tokenizer_artifact(path: str, *, sha256: str) -> str:
    if not path or len(sha256) != 64:
        raise ValueError("configure a local tokenizer path and SHA-256")
    with Path(path).open("rb") as stream:
        data = stream.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("tokenizer artifact exceeds budget")
    if hashlib.sha256(data).hexdigest() != sha256.lower():
        raise ValueError("tokenizer artifact checksum mismatch")
    return data.decode("utf-8")
