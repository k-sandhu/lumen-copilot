"""One offline harness per connector — the fixtures the conformance rules need.

A rule can only bite if it can *run* the connector, and a connector can only be
run against its source. So each connector contributes a harness: a source row, a
framework-shaped :class:`ConnectorRun` over an ``httpx.MockTransport`` (never a
socket), the invalid configs its ``validate_config`` must reject, and — per
declared capability — cursor scenarios and ACL fixtures.

**A registered connector without a harness fails the kit.** That is deliberate:
the SDK's promise is "drop in a package and the framework does the rest", so the
matching obligation is "prove it here". Adding a connector is therefore a
two-file change: ``app/connectors/<name>/`` and a harness below.

Everything here is offline and hermetic:

* the Drive double is a scriptable dict-backed fake behind the **real** guarded
  client (:func:`app.connectors.oauth.build_authenticated_client`), with the
  shared egress primitive stubbed to a public-range answer — the guard's order
  (https → pinned host → resolve/range → pin) runs for real, no DNS, no socket;
* the web double replaces the client the connector constructs for itself, using
  a public IP literal so nothing resolves either.

This is a *separate*, deliberately small double from the one in
``test_gdrive_connector.py``: that file proves Drive behaviour in depth, this one
only needs enough Drive to exercise the SDK contract, and keeping them apart
means neither suite's fixtures constrain the other.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

import app.net.egress as net_egress
from app.connectors.base import AclMappingContext, ConnectorRun
from app.domain.entities import Source, SourceStatus
from tests.conformance.kit import AclCase

__all__ = ["ConnectorHarness", "harness_for", "harness_names"]

_GDOC = "application/vnd.google-apps.document"
_FOLDER_MIME = "application/vnd.google-apps.folder"

# A routable public literal: the web connector's SSRF guard accepts it without
# a DNS lookup, so the offline harness never resolves anything.
_PUBLIC_IP = "93.184.216.34"


def _source(source_type: str, config: dict[str, object]) -> Source:
    now = datetime.now(UTC)
    return Source(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        type=source_type,
        config=config,
        status=SourceStatus.PENDING,
        indexed_count=0,
        last_synced_at=None,
        last_error=None,
        created_at=now,
        updated_at=now,
    )


@dataclass
class ConnectorHarness:
    """Everything the kit needs to exercise one connector offline.

    Capability fixtures are optional and are only consumed when the connector
    actually declares the matching capability, so a credential-less connector
    like ``web`` carries none of them.
    """

    name: str
    valid_config: dict[str, object]
    invalid_configs: tuple[dict[str, object], ...]
    # The exact stable `code` each fault path must report, every time. Declared
    # here rather than inferred so a churned code is a visible diff, not a
    # silently-accepted change (ADR-0009 §1: the code is the API's contract).
    sync_fault_code: str | None = None
    changes_fault_code: str | None = None
    # fetch_changes fixtures (ADR-0019 §3)
    start_cursor: str | None = None
    expired_cursor: str | None = None
    fault_cursor: str | None = None
    cascade_cursor: str | None = None
    cascade_scope_id: str | None = None
    incomplete_cursor: str | None = None
    # map_acl fixtures (ADR-0019 §2)
    acl_context: AclMappingContext | None = None
    acl_cases: tuple[AclCase, ...] = ()

    def source(self) -> Source:
        return _source(self.name, dict(self.valid_config))

    @asynccontextmanager
    async def run(self, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ConnectorRun]:
        """The framework-shaped per-run context (ADR-0019 §4)."""
        raise NotImplementedError  # pragma: no cover — overridden per connector
        yield  # pragma: no cover — makes the base an async generator for typing

    @asynccontextmanager
    async def faulting_run(self, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ConnectorRun]:
        """A run whose provider **fails** — for the typed-fault rules.

        Every connector must have one: the ADR's "typed ConnectorError mapping"
        is about the faults a real run hits, not only about a rejected config.
        """
        raise NotImplementedError  # pragma: no cover — overridden per connector
        yield  # pragma: no cover — makes the base an async generator for typing


# --- web ---------------------------------------------------------------------


class _WebHarness(ConnectorHarness):
    """The credential-less connector: no OAuth, no cursor, no ACL mirror.

    Its ``ConnectorRun.http`` is anonymous and deliberately never dialled — the
    web connector keeps its own SSRF-guarded fetch chokepoint (ADR-0019 §4: "the
    web connector simply ignores the auth capability of its run"), so the run's
    transport answers 500 to make an accidental use loud.
    """

    @asynccontextmanager
    async def run(self, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ConnectorRun]:
        def _page(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=(
                    "<html><head><title>Conformance</title></head>"
                    "<body><p>hello from the harness</p></body></html>"
                ),
            )

        async with self._patched(monkeypatch, _page) as run:
            yield run

    @asynccontextmanager
    async def faulting_run(self, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ConnectorRun]:
        """The source URL answers 503 — the web connector's `fetch_failed` path."""

        def _boom(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                503, headers={"content-type": "text/html"}, text="<p>upstream is down</p>"
            )

        async with self._patched(monkeypatch, _boom) as run:
            yield run

    @asynccontextmanager
    async def _patched(
        self,
        monkeypatch: pytest.MonkeyPatch,
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> AsyncIterator[ConnectorRun]:
        # Built BEFORE the class patch below, so the anonymous run client is a
        # real client with its own explicit transport.
        anonymous = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
        real_client = httpx.AsyncClient

        def _factory(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
            return real_client(transport=httpx.MockTransport(handler), follow_redirects=False)

        monkeypatch.setattr("app.connectors.web.connector.httpx.AsyncClient", _factory)
        try:
            yield ConnectorRun(http=anonymous)
        finally:
            await anonymous.aclose()


_WEB = _WebHarness(
    name="web",
    valid_config={"url": f"http://{_PUBLIC_IP}/page"},
    invalid_configs=(
        {},
        {"url": ""},
        {"url": 123},
        {"url": "ftp://example.com/x"},
        {"url": "http://127.0.0.1/x"},
        {"url": "http://169.254.169.254/latest/meta-data"},
    ),
    sync_fault_code="fetch_failed",
)


# --- gdrive ------------------------------------------------------------------


class _FakeDrive:
    """A minimal scriptable Drive REST v3 double (dict in, JSON out).

    Only the endpoints the SDK contract needs: the start token, an enumeration
    (parent-aware, so a cascade re-examination really walks a subtree),
    per-file permissions + export, change-log replays, and ``about.get``. An
    unknown change token answers an empty terminal page whose
    ``newStartPageToken`` derives from it — which is what makes the cursor
    round-trip assertion meaningful rather than scripted.

    ``fail_all`` turns every request into a 500 (the typed-fault scenario);
    ``fault_tokens`` fails only a specific ``changes.list`` token, so an
    ordinary replay fault can be told apart from a dead cursor.
    """

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.permissions: dict[str, list[dict[str, Any]]] = {}
        self.exports: dict[str, bytes] = {}
        self.changes_pages: dict[str, dict[str, Any]] = {}
        self.expired_tokens: set[str] = set()
        self.fault_tokens: set[str] = set()
        self.fail_all = False
        self.start_token = "baseline-0"

    def add_doc(
        self,
        file_id: str,
        *,
        name: str,
        permissions: list[dict[str, Any]],
        body: bytes,
        parent: str = "root-folder",
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "id": file_id,
            "name": name,
            "mimeType": _GDOC,
            "modifiedTime": "2026-07-18T10:00:00Z",
            "parents": [parent],
        }
        self.files[file_id] = entry
        self.permissions[file_id] = permissions
        self.exports[file_id] = body
        return entry

    def add_folder(self, folder_id: str, *, parent: str | None = None) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "id": folder_id,
            "name": folder_id,
            "mimeType": _FOLDER_MIME,
            "parents": [parent] if parent else [],
        }
        self.files[folder_id] = entry
        self.permissions[folder_id] = []
        return entry

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_all:
            return httpx.Response(500, json={"error": "drive is unwell"})
        path = request.url.path
        params = request.url.params
        if path.endswith("/changes/startPageToken"):
            return self._json({"startPageToken": self.start_token})
        if path.endswith("/drive/v3/changes"):
            token = params.get("pageToken", "")
            if token in self.expired_tokens:
                return httpx.Response(410, json={"error": "expired"})
            if token in self.fault_tokens:
                return httpx.Response(500, json={"error": "transient"})
            page = self.changes_pages.get(token)
            if page is None:
                page = {"changes": [], "newStartPageToken": f"{token}-next"}
            return self._json(page)
        if path.endswith("/drive/v3/about"):
            return self._json({"user": {"emailAddress": "admin@acme.test"}})
        if path.endswith("/drive/v3/files"):
            return self._json({"files": self._list(params.get("q", ""))})
        if path.endswith("/permissions"):
            file_id = path.split("/files/")[1].split("/")[0]
            return self._json({"permissions": self.permissions.get(file_id, [])})
        if path.endswith("/export"):
            file_id = path.split("/files/")[1].split("/")[0]
            return httpx.Response(200, content=self.exports.get(file_id, b""))
        if "/files/" in path:
            file_id = path.rsplit("/", 1)[1]
            entry = self.files.get(file_id)
            if entry is None:
                return httpx.Response(404, json={"error": "not found"})
            return self._json(dict(entry))
        return httpx.Response(404, json={"error": "unrouted"})  # pragma: no cover

    def _list(self, query: str) -> list[dict[str, Any]]:
        """Honour ``'<id>' in parents`` so a subtree walk really is a subtree.

        Without this every enumeration returns the whole corpus, and a cascade
        re-examination would "succeed" by accident regardless of the scope the
        connector asked for.
        """
        if " in parents" in query:
            parent = query.split("'")[1]
            return [
                dict(entry)
                for entry in self.files.values()
                if parent in (entry.get("parents") or [])
            ]
        return [dict(entry) for entry in self.files.values()]

    @staticmethod
    def _json(payload: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200, content=json.dumps(payload), headers={"content-type": "application/json"}
        )


def _build_fake_drive() -> _FakeDrive:
    fake = _FakeDrive()
    fake.add_folder("root-folder")
    fake.add_folder("cascade-folder", parent="root-folder")
    fake.add_doc(
        "doc-conformance",
        name="Conformance Doc",
        permissions=[
            {"type": "user", "role": "reader", "emailAddress": "alice@acme.test"},
            # Denied by the §2 effective-read rules (unexpanded group) — present
            # so a real sync exercises the fail-closed path too.
            {"type": "group", "role": "reader", "emailAddress": "team@acme.test"},
        ],
        body=b"conformance body",
    )
    # A document INSIDE the cascade folder, so the container change has a real
    # descendant to re-examine.
    fake.add_doc(
        "doc-nested",
        name="Nested Doc",
        permissions=[{"type": "user", "role": "reader", "emailAddress": "alice@acme.test"}],
        body=b"nested body",
        parent="cascade-folder",
    )

    # A plain replay: one upsert + one removal, integrity complete.
    fake.changes_pages["cursor-1"] = {
        "changes": [
            {
                "changeType": "file",
                "fileId": "doc-conformance",
                "file": dict(fake.files["doc-conformance"]),
            },
            {"changeType": "file", "fileId": "doc-removed", "removed": True},
        ],
        "newStartPageToken": "baseline-1",
    }
    # A CONTAINER permission change: Drive emits no per-descendant event, so the
    # connector must surface the folder id in stale_scope_ids and re-examine
    # what is under it (ADR-0019 §3 cascade).
    fake.changes_pages["cursor-cascade"] = {
        "changes": [
            {
                "changeType": "file",
                "fileId": "cascade-folder",
                "file": dict(fake.files["cascade-folder"]),
            }
        ],
        "newStartPageToken": "baseline-cascade",
    }
    # A live change with NO file metadata: the connector cannot prove what the
    # change did to the mirror, so the page must fail closed.
    fake.changes_pages["cursor-incomplete"] = {
        "changes": [{"changeType": "file", "fileId": "doc-mystery"}],
        "newStartPageToken": "baseline-incomplete",
    }
    fake.expired_tokens.add("cursor-expired")
    fake.fault_tokens.add("cursor-fault")
    return fake


_ALICE = uuid.uuid4()
_GDRIVE_CTX = AclMappingContext(
    email_to_user_id={"alice@acme.test": _ALICE},
    evaluated_at=datetime(2026, 7, 18, tzinfo=UTC),
)


class _GdriveHarness(ConnectorHarness):
    """The managed connector: OAuth + incremental cursor + ACL mirror.

    ``run.http`` is the **real** framework client
    (:func:`~app.connectors.oauth.build_authenticated_client`) wrapped around the
    fake, so the connector is exercised exactly as the sync task would build it —
    bearer held inside the transport, host pinned to ``oauth_spec().allowed_hosts``.
    """

    @asynccontextmanager
    async def run(self, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ConnectorRun]:
        async with self._client(monkeypatch, _build_fake_drive()) as http:
            yield ConnectorRun(http=http, acl_context=_GDRIVE_CTX)

    @asynccontextmanager
    async def faulting_run(self, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ConnectorRun]:
        """Drive answers 500 to everything — the `drive_api_error` path."""
        fake = _build_fake_drive()
        fake.fail_all = True
        async with self._client(monkeypatch, fake) as http:
            yield ConnectorRun(http=http, acl_context=_GDRIVE_CTX)

    @asynccontextmanager
    async def _client(
        self, monkeypatch: pytest.MonkeyPatch, fake: _FakeDrive
    ) -> AsyncIterator[httpx.AsyncClient]:
        from app.connectors.oauth import build_authenticated_client
        from app.connectors.registry import get_connector

        # Public-range resolve + passthrough pin: the REAL guard runs, offline.
        monkeypatch.setattr(net_egress, "resolve_safe_ip", lambda host: "142.250.4.95")
        monkeypatch.setattr(net_egress, "pin_url_to_ip", lambda url, ip: url)
        spec = get_connector("gdrive").oauth_spec()  # type: ignore[attr-defined]
        client = build_authenticated_client(
            spec,
            access_token="conformance-token",
            timeout=5.0,
            inner_transport=httpx.MockTransport(fake.handler),
        )
        try:
            yield client
        finally:
            await client.aclose()


_GDRIVE = _GdriveHarness(
    name="gdrive",
    valid_config={"mode": "my_drive"},
    invalid_configs=(
        {},
        {"mode": "everything"},
        {"mode": "my_drive", "folder_id": "abc"},
        {"mode": "folder"},
        {"mode": "shared_drive"},
        {"mode": "shared_drive", "drive_id": "d1", "folder_id": "f1"},
    ),
    sync_fault_code="drive_api_error",
    changes_fault_code="drive_api_error",
    start_cursor="cursor-1",
    expired_cursor="cursor-expired",
    fault_cursor="cursor-fault",
    cascade_cursor="cursor-cascade",
    cascade_scope_id="cascade-folder",
    incomplete_cursor="cursor-incomplete",
    acl_context=_GDRIVE_CTX,
    acl_cases=(
        AclCase(
            label="attested reader maps to that user only",
            raw={
                "permissions": [
                    {"type": "user", "role": "reader", "emailAddress": "alice@acme.test"},
                    {"type": "user", "role": "reader", "emailAddress": "bob@acme.test"},
                ]
            },
            # The source allows both readers; only the attested one is mappable,
            # so the mirror is a strict subset (deliberate under-sharing, §2).
            source_allow=frozenset({f"user:{_ALICE}", "user:<bob-unattested>"}),
            expected=frozenset({f"user:{_ALICE}"}),
        ),
        AclCase(
            label="link-public maps to the tenant principal",
            raw={"permissions": [{"type": "anyone", "role": "reader"}]},
            source_allow=frozenset({"tenant"}),
            expected=frozenset({"tenant"}),
        ),
        AclCase(
            label="domain share grants nobody in v1",
            raw={"permissions": [{"type": "domain", "role": "reader", "domain": "acme.test"}]},
            source_allow=frozenset({"tenant"}),
            expected=frozenset(),
        ),
        AclCase(
            label="unexpanded group grants nobody",
            raw={
                "permissions": [
                    {"type": "group", "role": "reader", "emailAddress": "team@acme.test"}
                ]
            },
            source_allow=frozenset({f"user:{_ALICE}"}),
            expected=frozenset(),
        ),
        AclCase(
            label="expiring share grants nobody (no acl_expires_at in v1)",
            raw={
                "permissions": [
                    {
                        "type": "user",
                        "role": "reader",
                        "emailAddress": "alice@acme.test",
                        "expirationTime": "2030-01-01T00:00:00Z",
                    }
                ]
            },
            source_allow=frozenset({f"user:{_ALICE}"}),
            expected=frozenset(),
        ),
        AclCase(
            label="metadata-only view grants no content access",
            raw={
                "permissions": [
                    {
                        "type": "user",
                        "role": "reader",
                        "emailAddress": "alice@acme.test",
                        "view": "metadata",
                    }
                ]
            },
            source_allow=frozenset({f"user:{_ALICE}"}),
            expected=frozenset(),
        ),
    ),
)


_HARNESSES: dict[str, ConnectorHarness] = {_WEB.name: _WEB, _GDRIVE.name: _GDRIVE}


def harness_names() -> frozenset[str]:
    """Connector names with a conformance harness."""
    return frozenset(_HARNESSES)


def harness_for(name: str) -> ConnectorHarness:
    """The harness for ``name``, or an actionable failure.

    The message is the onboarding instruction for the next connector author: the
    kit is parametrized over the *real* registry, so dropping a connector in
    without a harness is caught the moment the suite runs.
    """
    harness = _HARNESSES.get(name)
    assert harness is not None, (
        f"connector {name!r} is registered but has no conformance harness — add one in "
        "backend/tests/conformance/harnesses.py (an offline source + ConnectorRun, the "
        "invalid configs it must reject, and a fixture per declared capability). See "
        "docs/guides/building-a-connector.md."
    )
    return harness
