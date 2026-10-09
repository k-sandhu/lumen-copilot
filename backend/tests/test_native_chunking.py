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


def test_real_bridge_uses_local_tokenizer_and_fingerprints_generation(tmp_path) -> None:
    pytest.importorskip("lumen_docintel")
    from app.core.config import Settings
    from app.ingestion.native import chunk_canonical, render_canonical

    vocab = {"[UNK]": 0} | {c: n + 1 for n, c in enumerate("ab .é🙂")}
    artifact = json.dumps(
        {
            "version": "1.0",
            "truncation": None,
            "padding": None,
            "added_tokens": [],
            "normalizer": None,
            "pre_tokenizer": None,
            "post_processor": None,
            "decoder": None,
            "model": {
                "type": "BPE",
                "dropout": None,
                "unk_token": "[UNK]",
                "continuing_subword_prefix": None,
                "end_of_word_suffix": None,
                "fuse_unk": False,
                "byte_fallback": False,
                "vocab": vocab,
                "merges": [],
            },
        },
        ensure_ascii=False,
    ).encode()
    path = tmp_path / "tiny-tokenizer.json"
    path.write_bytes(artifact)
    settings = Settings(
        native_ingestion_tokenizer_path=str(path),
        native_ingestion_tokenizer_sha256=hashlib.sha256(artifact).hexdigest(),
        native_ingestion_tokenizer_model="fixture",
        LLM_EMBEDDING_MODEL="fixture",
        native_ingestion_chunk_tokens=128,
        native_ingestion_chunk_chars=128,
        native_ingestion_overlap_chars=20,
    )
    source = "a" * 80 + ". " + "é🙂b" * 100
    document = render_canonical(json.dumps({"blocks": [{"id": "b", "text": source}]}))
    result = chunk_canonical(document, settings=settings)
    assert len(result.chunks) > 1
    assert all(source[c.char_start : c.char_end] == c.text for c in result.chunks)
    generation = json.loads(result.document.generation_json)
    assert hashlib.sha256(artifact).hexdigest() in generation["tokenizer_id"]
    assert generation["fingerprint"]["native_chunker"]["settings"]["embedding_model"] == "fixture"
