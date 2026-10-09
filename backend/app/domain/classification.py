"""Tenant-bound operational snapshots; domain values contain no credentials."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

PROMPT_VERSION = "classification-1.1"


@dataclass(frozen=True, slots=True)
class ClassificationWork:
    tenant_id: UUID
    document_id: UUID
    input_fingerprint: str
    extraction_id: str
    taxonomy_version: str
    input_json: str
    result: dict[str, Any]
    status: str
    revision: int
    retries: int
    run_id: UUID | None
    override_path: str | None
