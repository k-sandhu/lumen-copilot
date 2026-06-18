"""Collections service unit tests — the cursor codec (#46).

Focused tests for the opaque keyset-cursor codec in
``app.services.collections_service``: a round-trip preserves the boundary
collection id, and a malformed cursor is rejected fail-closed as a
:class:`~app.core.errors.ValidationError` (INV-8 → 422) rather than silently
falling back to the first page. The end-to-end pagination behaviour is covered
against the real app in ``test_collections_api``.
"""

from __future__ import annotations

import base64
import uuid

import pytest

from app.core.errors import ValidationError
from app.services.collections_service import (
    _clamp_limit,
    _decode_cursor,
    _encode_cursor,
)


def test_cursor_round_trips_collection_id() -> None:
    cid = uuid.uuid4()
    assert _decode_cursor(_encode_cursor(cid)) == cid


def test_cursor_is_opaque_base64() -> None:
    cid = uuid.uuid4()
    cursor = _encode_cursor(cid)
    # Opaque to the wire: not the bare uuid, and URL-safe.
    assert cursor != str(cid)
    assert cursor == cursor.strip()


@pytest.mark.parametrize(
    "bad",
    [
        "not-base64!!!",
        "",
        "Zm9vYmFy",  # base64 of "foobar" — no "col:" prefix
    ],
)
def test_malformed_cursor_raises_validation_error(bad: str) -> None:
    with pytest.raises(ValidationError):
        _decode_cursor(bad)


def test_cursor_with_prefix_but_bad_uuid_raises() -> None:
    encoded = base64.urlsafe_b64encode(b"col:not-a-uuid").decode()
    with pytest.raises(ValidationError):
        _decode_cursor(encoded)


def test_cursor_without_prefix_raises() -> None:
    # A valid uuid but minted without our prefix is rejected (not one of ours).
    encoded = base64.urlsafe_b64encode(str(uuid.uuid4()).encode()).decode()
    with pytest.raises(ValidationError):
        _decode_cursor(encoded)


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (None, 20),  # default
        (1, 1),
        (100, 100),
        (0, 1),  # below floor → clamped up
        (-5, 1),
        (250, 100),  # above ceiling → clamped down
    ],
)
def test_clamp_limit(requested: int | None, expected: int) -> None:
    assert _clamp_limit(requested) == expected
