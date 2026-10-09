from __future__ import annotations

import hashlib
import json

import pytest

from app.domain.native_chunking import ChunkedDocument
from app.ingestion.tokenizer_artifact import load_tokenizer_artifact


def test_local_tokenizer_checksum_and_limit(tmp_path) -> None:
    artifact = tmp_path / "tokenizer.json"
    data = b'{"fixture":true}'
    artifact.write_bytes(data)
    assert (
        load_tokenizer_artifact(str(artifact), sha256=hashlib.sha256(data).hexdigest())
        == data.decode()
    )
    with pytest.raises(ValueError, match="checksum"):
        load_tokenizer_artifact(str(artifact), sha256="a" * 64)
    artifact.write_bytes(b" " * (32 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="budget"):
        load_tokenizer_artifact(str(artifact), sha256="a" * 64)


def test_facade_rejects_false_evidence_offsets() -> None:
    rendered = {
        "document": {
            "schema_version": 1,
            "renderer_version": 1,
            "blocks": [],
            "source_parts": [],
            "generation": {},
        },
        "rendered_text": "é🙂",
        "spans": [],
    }
    chunk = {
        "ord": 0,
        "text": "wrong",
        "char_start": 0,
        "char_end": 1,
        "block_id": "b",
        "context": "header",
        "context_cell_indices": [],
        "token_count": 2,
    }
    with pytest.raises(ValueError, match="evidence"):
        ChunkedDocument.from_json(json.dumps({"rendered": rendered, "chunks": [chunk]}))
