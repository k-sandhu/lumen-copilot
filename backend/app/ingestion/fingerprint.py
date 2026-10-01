"""Reproducible native extraction provenance, with no provider or parser imports."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from importlib.metadata import version
from importlib.resources import files

from app.domain.llm import Embedding
from app.ingestion.parsers import SUPPORTED_MIME_TYPES

_DISTRIBUTIONS = {
    "application/pdf": "pypdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "python-docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "python-pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "openpyxl",
}


class FingerprintError(ValueError):
    """Cannot truthfully describe this extraction generation."""


def _implementation_hash(module: str) -> str:
    # Source files are shipped in the application wheel. Normalize checkout line
    # endings so the same implementation has the same build identity on each OS.
    source = files("app.ingestion").joinpath(module).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(source).hexdigest()


def build_ingestion_fingerprint(
    data: bytes,
    *,
    mime_type: str,
    chunk_size: int,
    overlap: int,
    embeddings: Sequence[Embedding],
) -> dict[str, object]:
    normalized = mime_type.split(";", 1)[0].strip().lower()
    if normalized not in SUPPORTED_MIME_TYPES:
        raise FingerprintError("unsupported native parser MIME type")
    if (
        isinstance(chunk_size, bool)
        or isinstance(overlap, bool)
        or chunk_size <= 0
        or not 0 <= overlap < chunk_size
    ):
        raise FingerprintError("invalid chunker settings")
    actual_model: str | None = None
    dimension: int | None = None
    for embedding in embeddings:
        if not isinstance(embedding.model, str) or not embedding.model.strip():
            raise FingerprintError("embedding result has no model identity")
        if not embedding.vector or any(
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            for value in embedding.vector
        ):
            raise FingerprintError("embedding result has no finite nonempty vector")
        if actual_model is None:
            actual_model = embedding.model
            dimension = len(embedding.vector)
        elif embedding.model != actual_model or len(embedding.vector) != dimension:
            raise FingerprintError("embedding results disagree on model or dimension")
    try:
        distribution = _DISTRIBUTIONS.get(normalized)
        dependencies = {distribution: version(distribution)} if distribution else {}
        parser_hash = _implementation_hash("parsers.py")
        chunker_hash = _implementation_hash("chunking.py")
    except (OSError, ValueError, ImportError) as exc:
        raise FingerprintError("native implementation build metadata is unavailable") from exc
    return {
        "schema_version": 1,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "mime_type": normalized,
        "parser_version": "native-1",
        "parser_code_sha256": parser_hash,
        "parser_dependencies": dependencies,
        "chunker_version": "boundary-1",
        "chunker_code_sha256": chunker_hash,
        "chunk_size": chunk_size,
        "chunk_overlap": overlap,
        "embedding_model": actual_model,
        "embedding_dimension": dimension,
    }
