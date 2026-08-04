"""Shared HTTP client for benchmark tooling — login, token refresh, uploads (#441).

Both the eval runner (:mod:`tests.eval.benchmark.run_live`) and the data-pack
loader (:mod:`tests.eval.benchmark.load_pack`) drive the real stack over HTTP as
a signed-in user. This module owns the mechanics they share so neither
copy-pastes them:

* **Login + token refresh.** Access tokens are capped at 15 minutes
  (``ACCESS_TOKEN_TTL_SECONDS``) while corpus ingestion takes longer; every
  request goes through :meth:`ApiClient.request`, which re-logs-in once on a
  401 and replays the call. Uploads therefore send re-sendable *bytes*, never
  consumed file handles.
* **Collection + document listing helpers** (cursor-paged), and a bounded
  ready/failed **ingestion wait** that tolerates transient poll errors without
  spinning on a permanent one.

Sync httpx + polling is deliberate: this is host-side test tooling driving a
server, not app code on the event loop.
"""

from __future__ import annotations

import time

import httpx

_POLL_SECONDS = 3.0
# Consecutive-failure cap for the ingestion-wait poll: transient blips ride
# through; a persistently failing listing (downed stack, revoked user) aborts.
_MAX_CONSECUTIVE_POLL_ERRORS = 20


class ApiClient:
    """A signed-in HTTP session against one stack, hardened for long runs."""

    def __init__(self, client: httpx.Client, api: str, email: str, password: str) -> None:
        self._client = client
        self._api = api.rstrip("/")
        self._email = email
        self._password = password

    def login(self) -> None:
        response = self._client.post(
            f"{self._api}/api/v1/auth/login",
            json={"email": self._email, "password": self._password},
        )
        response.raise_for_status()
        self._client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"

    def request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        """An authed request that survives access-token expiry (one re-login)."""
        response = self._client.request(method, f"{self._api}{path}", **kwargs)  # type: ignore[arg-type]
        if response.status_code == 401:
            self.login()
            response = self._client.request(method, f"{self._api}{path}", **kwargs)  # type: ignore[arg-type]
        return response

    # --- Collections / documents ---------------------------------------------

    def ensure_collection(self, name: str) -> str:
        """Return the id of the caller's collection ``name``, creating it if absent."""
        listing = self.request("GET", "/api/v1/collections")
        listing.raise_for_status()
        for item in listing.json().get("items", []):
            if item["name"] == name:
                return str(item["id"])
        created = self.request("POST", "/api/v1/collections", json={"name": name})
        created.raise_for_status()
        return str(created.json()["id"])

    def existing_documents(
        self, *, collection_id: str | None = None
    ) -> dict[str, dict[str, object]]:
        """Map filename -> document row, **scoped to one collection** when given.

        A profile-wide map keyed only by filename is dangerous for the callers
        that act on a match: the same filename can legitimately exist in another
        collection, and treating that row as "ours" means skipping an upload
        that never happened — or, worse, deleting someone else's document during
        a rolling refresh. Callers that are about to mutate MUST pass
        ``collection_id``; rows outside it are then invisible.
        """
        docs: dict[str, dict[str, object]] = {}
        cursor: str | None = None
        while True:
            params: dict[str, str] = {"limit": "100"}
            if collection_id:
                params["collection_id"] = collection_id
            if cursor:
                params["cursor"] = cursor
            response = self.request("GET", "/api/v1/documents", params=params)
            response.raise_for_status()
            payload = response.json()
            for item in payload.get("items", []):
                # Belt and braces: even with the server-side filter, never let a
                # row from another collection into a map the caller may delete from.
                if collection_id and str(item.get("collection_id", "")) != collection_id:
                    continue
                docs[str(item["filename"])] = item
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        return docs

    def upload_document(
        self, *, collection_id: str, filename: str, data: bytes, mime_type: str
    ) -> httpx.Response:
        """POST one multipart upload (bytes, so a 401-refresh replay can re-send)."""
        return self.request(
            "POST",
            "/api/v1/documents",
            data={"collection_id": collection_id},
            files={"file": (filename, data, mime_type)},
        )

    def delete_document(self, document_id: str) -> None:
        """DELETE one document (204) — used by the loader's rolling refresh (#443)."""
        response = self.request("DELETE", f"/api/v1/documents/{document_id}")
        if response.status_code != 204:
            response.raise_for_status()

    def documents_by_id(self, *, collection_id: str) -> dict[str, dict[str, object]]:
        """Map document id -> row for one collection.

        Keyed by **id**, not filename: a filename is not unique across a profile
        (or even within a collection over time), so anything that waits on or
        acts upon a specific document must address it by the id the upload
        returned.
        """
        rows: dict[str, dict[str, object]] = {}
        cursor: str | None = None
        while True:
            params: dict[str, str] = {"limit": "100", "collection_id": collection_id}
            if cursor:
                params["cursor"] = cursor
            response = self.request("GET", "/api/v1/documents", params=params)
            response.raise_for_status()
            payload = response.json()
            for item in payload.get("items", []):
                if str(item.get("collection_id", "")) != collection_id:
                    continue
                rows[str(item["id"])] = item
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        return rows

    def wait_for_documents(
        self, document_ids: set[str], *, collection_id: str, timeout_seconds: float
    ) -> dict[str, str]:
        """Poll until every **document id** reaches ready/failed (or the deadline).

        Returns id -> terminal status (``"timeout"`` for stragglers, ``"missing"``
        never terminal — a row that vanishes keeps being polled until timeout).

        Waiting by id rather than filename is load-bearing: the previous version
        matched on filename against an unscoped listing, so an OLD copy of the
        same file — or a same-named document elsewhere in the profile — could
        satisfy the wait for a brand-new upload. The caller would then believe
        ingestion had succeeded and, on the refresh path, delete the old
        document while its replacement was still pending or already failed.
        """
        deadline = time.monotonic() + timeout_seconds
        outcomes: dict[str, str] = {}
        pending = set(document_ids)
        consecutive_errors = 0
        while pending and time.monotonic() < deadline:
            try:
                by_id = self.documents_by_id(collection_id=collection_id)
                consecutive_errors = 0
            except httpx.HTTPError as exc:
                consecutive_errors += 1
                if consecutive_errors > _MAX_CONSECUTIVE_POLL_ERRORS:
                    raise RuntimeError(
                        f"documents listing failed {consecutive_errors} times in a row"
                    ) from exc
                print(f"[  poll] transient documents-list error, retrying: {exc}")
                time.sleep(_POLL_SECONDS)
                continue
            for document_id in list(pending):
                row = by_id.get(document_id)
                if row is None:
                    continue  # not visible yet — keep waiting, never call it done
                status = str(row["status"])
                if status in {"ready", "failed"}:
                    outcomes[document_id] = status
                    pending.discard(document_id)
                    marker = "ready " if status == "ready" else "FAILED"
                    print(f"[{marker}] {row.get('filename', document_id)}")
            if pending:
                time.sleep(_POLL_SECONDS)
        for document_id in pending:
            outcomes[document_id] = "timeout"
        return outcomes


__all__ = ["ApiClient"]
