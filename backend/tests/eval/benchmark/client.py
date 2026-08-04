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

    def existing_documents(self) -> dict[str, dict[str, object]]:
        """Map filename -> document row for everything visible to the caller."""
        docs: dict[str, dict[str, object]] = {}
        cursor: str | None = None
        while True:
            params: dict[str, str] = {"limit": "100"}
            if cursor:
                params["cursor"] = cursor
            response = self.request("GET", "/api/v1/documents", params=params)
            response.raise_for_status()
            payload = response.json()
            for item in payload.get("items", []):
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

    def wait_for_documents(self, filenames: set[str], *, timeout_seconds: float) -> dict[str, str]:
        """Poll until every filename reaches ready/failed (or the deadline).

        Returns filename -> terminal status (``"timeout"`` for stragglers).
        Transient listing failures are tolerated up to a consecutive cap.
        """
        deadline = time.monotonic() + timeout_seconds
        outcomes: dict[str, str] = {}
        pending = set(filenames)
        consecutive_errors = 0
        while pending and time.monotonic() < deadline:
            try:
                by_filename = self.existing_documents()
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
            for filename in list(pending):
                row = by_filename.get(filename)
                status = str(row["status"]) if row else "missing"
                if status in {"ready", "failed"}:
                    outcomes[filename] = status
                    pending.discard(filename)
                    marker = "ready " if status == "ready" else "FAILED"
                    print(f"[{marker}] {filename}")
            if pending:
                time.sleep(_POLL_SECONDS)
        for filename in pending:
            outcomes[filename] = "timeout"
        return outcomes


__all__ = ["ApiClient"]
