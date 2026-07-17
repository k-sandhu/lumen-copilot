"""Data-pack loader guarantees (#441) — deterministic selection + resilient client.

The selection rule is the loader's contract: same inputs ⇒ same files, balanced
across the chosen formats, never a negative-format entry. The client tests pin
the 401-refresh replay that long ingestion waits depend on.
"""

from __future__ import annotations

import httpx
import pytest

from tests.eval.benchmark.client import ApiClient
from tests.eval.benchmark.load_pack import FORMAT_MIME, select_pack
from tests.eval.benchmark.manifest import CORPUS

# --- Deterministic selection ---------------------------------------------------


def test_selection_is_deterministic() -> None:
    """Same flags twice ⇒ byte-identical selection."""
    first = select_pack(["pdf", "xlsx"], 5)
    second = select_pack(["pdf", "xlsx"], 5)
    assert [e.file_id for e in first] == [e.file_id for e in second]
    assert len(first) == 5


def test_round_robin_balances_formats() -> None:
    """count=6 over all formats ⇒ exactly one file of each of the six formats."""
    selection = select_pack(None, 6)
    mimes = [e.mime_type for e in selection]
    assert len(set(mimes)) == 6, f"expected one per format, got {mimes}"


def test_round_robin_follows_caller_format_order() -> None:
    """--formats xlsx,pdf --count 1 ⇒ an XLSX (the caller's first format)."""
    selection = select_pack(["xlsx", "pdf"], 1)
    assert selection[0].mime_type == FORMAT_MIME["xlsx"]


def test_single_format_takes_manifest_order() -> None:
    """A one-format selection is the first N of that format in manifest order."""
    expected = [
        e.file_id for e in CORPUS if e.expected_ingest == "ok" and e.mime_type == FORMAT_MIME["pdf"]
    ][:3]
    selection = select_pack(["pdf"], 3)
    assert [e.file_id for e in selection] == expected


def test_negative_format_entries_are_never_selected() -> None:
    """Even selecting everything, the deliberate CSV/HTML negatives stay out."""
    selection = select_pack(None, None)
    assert all(e.expected_ingest == "ok" for e in selection)
    eligible = sum(1 for e in CORPUS if e.expected_ingest == "ok")
    assert len(selection) == eligible


def test_count_beyond_eligible_caps_at_eligible() -> None:
    selection = select_pack(["pptx"], 99)
    eligible = sum(
        1 for e in CORPUS if e.expected_ingest == "ok" and e.mime_type == FORMAT_MIME["pptx"]
    )
    assert len(selection) == eligible


def test_unknown_format_and_bad_count_raise() -> None:
    """Misuse fails loudly — never a silent empty selection (INV-8 flavour)."""
    with pytest.raises(ValueError, match="unknown format"):
        select_pack(["csv"], 1)  # the negative format is not offerable
    with pytest.raises(ValueError, match="positive"):
        select_pack(["pdf"], 0)
    with pytest.raises(ValueError, match="at least one"):
        select_pack([" "], 1)


def test_duplicate_formats_collapse() -> None:
    a = select_pack(["pdf", "pdf", "PDF"], 2)
    b = select_pack(["pdf"], 2)
    assert [e.file_id for e in a] == [e.file_id for e in b]


# --- ApiClient 401-refresh replay ----------------------------------------------


def test_client_relogs_in_once_on_401() -> None:
    """An expired token mid-run triggers exactly one re-login and a replay."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(200, json={"access_token": f"tok{len(calls)}"})
        if request.headers.get("Authorization") == "Bearer tok1":
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, json={"items": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        api = ApiClient(http, "http://stack", "u@x.test", "pw")
        api.login()  # -> tok1
        response = api.request("GET", "/api/v1/documents")

    assert response.status_code == 200
    assert calls == [
        "POST /api/v1/auth/login",
        "GET /api/v1/documents",  # 401 with the stale token
        "POST /api/v1/auth/login",  # transparent re-login
        "GET /api/v1/documents",  # replay succeeds
    ]
