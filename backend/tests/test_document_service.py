"""Document service unit tests — cursor codec and pagination bounds (#28).

Focused tests for the pure helpers in ``app.services.document_service``:

* the opaque keyset-cursor codec round-trips the boundary document id and
  rejects a malformed cursor fail-closed (INV-8 → 422), like the collections
  codec — the end-to-end pagination is covered in ``test_documents_api``;
"""

from __future__ import annotations

import base64
import uuid

import pytest

from app.core.errors import ValidationError
from app.services.document_service import (
    _clamp_limit,
    _decode_cursor,
    _encode_cursor,
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
