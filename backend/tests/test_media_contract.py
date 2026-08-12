"""Frozen REST/WS contract for direct media uploads (spec 0008 / #571).

These tests deliberately inspect the canonical sources in ``contracts/``. They
pin the cross-tier hand-off before either implementation is allowed to guess.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _openapi() -> dict[str, Any]:
    value = yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _ws() -> dict[str, Any]:
    value = json.loads(
        (ROOT / "contracts" / "websocket-envelopes.schema.json").read_text(encoding="utf-8")
    )
    assert isinstance(value, dict)
    return value


def test_upload_control_plane_never_accepts_file_bytes() -> None:
    spec = _openapi()
    paths = spec["paths"]

    legacy = paths["/documents"]["post"]
    assert legacy.get("deprecated") is True
    assert "requestBody" not in legacy
    assert set(legacy["responses"]) == {"401", "410"}

    initiate = paths["/api/v2/document-uploads"]["post"]
    content = initiate["requestBody"]["content"]
    assert set(content) == {"application/json"}
    assert "multipart/form-data" not in json.dumps(initiate)
    request_ref = content["application/json"]["schema"]["$ref"]
    assert request_ref.endswith("/DocumentUploadCreate")
    schema = spec["components"]["schemas"]["DocumentUploadCreate"]
    assert schema["required"] == ["filename", "mime_type", "size_bytes", "collection_id"]
    assert all(prop.get("format") != "binary" for prop in schema["properties"].values())

    # The retired router is not merely hidden: its former service-level byte
    # ingress/egress seams are gone, preventing a later route from accidentally
    # reconnecting FastAPI to whole-file payloads.
    from app.services.document_service import DocumentService

    assert "upload" not in DocumentService.__dict__
    assert "get_content" not in DocumentService.__dict__


def test_multipart_session_contract_is_resumable_bounded_and_typed() -> None:
    spec = _openapi()
    paths = spec["paths"]
    required = {
        "/api/v2/document-uploads",
        "/api/v2/document-uploads/{uploadId}",
        "/api/v2/document-uploads/{uploadId}/parts",
        "/api/v2/document-uploads/{uploadId}/complete",
    }
    assert required <= set(paths)
    assert "get" in paths["/api/v2/document-uploads/{uploadId}"]
    assert "delete" in paths["/api/v2/document-uploads/{uploadId}"]

    schemas = spec["components"]["schemas"]
    session = schemas["DocumentUploadSession"]
    assert {
        "id",
        "document_id",
        "state",
        "filename",
        "mime_type",
        "size_bytes",
        "collection_id",
        "part_size_bytes",
        "part_count",
        "completed_parts",
        "expires_at",
        "document",
    } <= set(session["required"])
    assert set(schemas["DocumentUploadState"]["enum"]) == {
        "initiated",
        "completing",
        "completed",
        "aborted",
        "expired",
        "failed",
    }
    assert schemas["UploadPartNumberList"]["properties"]["part_numbers"]["maxItems"] <= 100
    assert schemas["CompleteDocumentUpload"]["properties"]["parts"]["maxItems"] == 10_000

    signed = schemas["SignedUploadPart"]
    assert {"part_number", "url", "expires_at", "required_headers"} <= set(signed["required"])
    assert signed["properties"]["url"]["format"] == "uri"

    for path, method in (
        ("/api/v2/document-uploads", "post"),
        ("/api/v2/document-uploads/{uploadId}", "get"),
        ("/api/v2/document-uploads/{uploadId}", "delete"),
        ("/api/v2/document-uploads/{uploadId}/parts", "post"),
        ("/api/v2/document-uploads/{uploadId}/complete", "post"),
    ):
        responses = paths[path][method]["responses"]
        assert "401" in responses
        if "{uploadId}" in path:
            assert "404" in responses


def test_media_access_and_transcript_are_json_control_plane_contracts() -> None:
    spec = _openapi()
    paths = spec["paths"]
    schemas = spec["components"]["schemas"]

    content = paths["/documents/{documentId}/content"]["get"]
    assert content.get("deprecated") is True
    assert set(content["responses"]) == {"401", "410"}

    access = paths["/api/v2/documents/{documentId}/access-url"]["post"]
    assert set(access["requestBody"]["content"]) == {"application/json"}
    response_ref = access["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert response_ref.endswith("/DocumentAccessUrl")
    assert {"url", "mime_type", "expires_at", "purpose"} <= set(
        schemas["DocumentAccessUrl"]["required"]
    )

    transcript = paths["/api/v2/documents/{documentId}/transcript"]["get"]
    assert {p["name"] for p in transcript["parameters"]} >= {"cursor", "limit", "around_ms"}
    assert {"401", "404", "409"} <= set(transcript["responses"])
    page = schemas["TranscriptPage"]
    assert {
        "document_id",
        "duration_ms",
        "language",
        "speakers",
        "items",
        "next_cursor",
    } <= set(page["required"])
    assert "transcription_model" in page["required"]
    segment = schemas["TranscriptSegment"]
    assert {"id", "ordinal", "speaker_id", "start_ms", "end_ms", "text"} <= set(segment["required"])
    speaker = schemas["TranscriptSpeaker"]
    assert {"speaker_id", "display_name", "name_status", "evidence_segment_ids"} <= set(
        speaker["required"]
    )


def test_document_and_every_citation_surface_carry_optional_media_time() -> None:
    spec = _openapi()
    schemas = spec["components"]["schemas"]

    assert set(schemas["DocumentKind"]["enum"]) == {"document", "audio", "video"}
    document = schemas["Document"]
    assert {"kind", "duration_ms"} <= set(document["properties"])

    for name in ("Citation", "SearchCitation"):
        properties = schemas[name]["properties"]
        assert {"time_start_ms", "time_end_ms", "transcript_segment_id"} <= set(properties)
        assert properties["time_start_ms"]["minimum"] == 0
        assert properties["time_end_ms"]["minimum"] == 1
        assert schemas[name]["dependentRequired"] == {
            "time_start_ms": ["time_end_ms"],
            "time_end_ms": ["time_start_ms"],
        }

    chat = _ws()["$defs"]["ChatCitation"]
    assert {"timeStartMs", "timeEndMs", "transcriptSegmentId"} <= set(chat["properties"])
    assert chat["properties"]["timeStartMs"]["minimum"] == 0
    assert chat["properties"]["timeEndMs"]["minimum"] == 1
    assert chat["dependentRequired"] == {
        "timeStartMs": ["timeEndMs"],
        "timeEndMs": ["timeStartMs"],
    }


def test_media_contract_paths_match_fastapi_mounts() -> None:
    """A canonical/generated client must call the routes FastAPI actually serves."""
    import re

    from app.main import create_app

    def route_shape(path: str) -> str:
        # Placeholder spelling is a generator concern (camelCase in the
        # hand-authored contract, snake_case in FastAPI); the HTTP route shape
        # and its /api/v2 mount are what must agree.
        return re.sub(r"\{[^}]+\}", "{}", path)

    canonical = {route_shape(path) for path in _openapi()["paths"] if path.startswith("/api/v2/")}
    emitted = {
        route_shape(path) for path in create_app().openapi()["paths"] if path.startswith("/api/v2/")
    }

    assert canonical == emitted
    assert not any(path.startswith("/v2/") for path in _openapi()["paths"])
