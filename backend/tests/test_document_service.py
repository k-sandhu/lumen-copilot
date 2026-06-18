"""Document service unit tests — cursor codec + upload-validation mapping (#28).

Focused tests for the pure helpers in ``app.services.document_service``:

* the opaque keyset-cursor codec round-trips the boundary document id and
  rejects a malformed cursor fail-closed (INV-8 → 422), like the collections
  codec — the end-to-end pagination is covered in ``test_documents_api``;
* ``_map_upload_validation_error`` re-maps the #22 storage ``ValidationError``
  to the contract's distinct upload statuses (``upload_too_large`` → 413,
  ``content_type_not_allowed`` → 415, anything else stays 422), so the API
  surfaces 413/415/422 from the single rule owner.
"""

from __future__ import annotations

import base64
import uuid

import pytest

from app.core.errors import (
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationError,
)
from app.services.document_service import (
    _clamp_limit,
    _decode_cursor,
    _encode_cursor,
    _map_upload_validation_error,
)

# --- Cursor codec -----------------------------------------------------------


def test_cursor_round_trips_document_id() -> None:
    did = uuid.uuid4()
    assert _decode_cursor(_encode_cursor(did)) == did


def test_cursor_is_opaque_base64() -> None:
    did = uuid.uuid4()
    cursor = _encode_cursor(did)
    assert cursor != str(did)
    assert cursor == cursor.strip()


@pytest.mark.parametrize(
    "bad",
    [
        "not-base64!!!",
        "",
        "Zm9vYmFy",  # base64 of "foobar" — no "doc:" prefix
    ],
)
def test_malformed_cursor_raises_validation_error(bad: str) -> None:
    with pytest.raises(ValidationError):
        _decode_cursor(bad)


def test_cursor_with_prefix_but_bad_uuid_raises() -> None:
    encoded = base64.urlsafe_b64encode(b"doc:not-a-uuid").decode()
    with pytest.raises(ValidationError):
        _decode_cursor(encoded)


def test_cursor_without_prefix_raises() -> None:
    encoded = base64.urlsafe_b64encode(str(uuid.uuid4()).encode()).decode()
    with pytest.raises(ValidationError):
        _decode_cursor(encoded)


def test_clamp_limit_bounds() -> None:
    assert _clamp_limit(None) == 20
    assert _clamp_limit(0) == 1
    assert _clamp_limit(1000) == 100
    assert _clamp_limit(37) == 37


# --- Upload-validation error mapping (413 / 415 / 422) ----------------------


def test_over_limit_maps_to_413() -> None:
    err = ValidationError("too big", code="upload_too_large")
    mapped = _map_upload_validation_error(err)
    assert isinstance(mapped, PayloadTooLargeError)
    assert mapped.status == 413


def test_disallowed_type_maps_to_415() -> None:
    err = ValidationError("nope", code="content_type_not_allowed")
    mapped = _map_upload_validation_error(err)
    assert isinstance(mapped, UnsupportedMediaTypeError)
    assert mapped.status == 415


def test_empty_upload_stays_422() -> None:
    err = ValidationError("empty", code="empty_upload")
    mapped = _map_upload_validation_error(err)
    # No distinct contract status: stays the original 422 ValidationError.
    assert mapped is err
    assert mapped.status == 422
