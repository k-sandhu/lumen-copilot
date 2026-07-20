"""Thin Drive REST v3 helpers — plain httpx, no Google SDK (ADR-0019 §4/§5).

Every function takes the **framework-supplied guarded client** (``run.http`` —
already authenticated, host-pinned to the connector's fixed allowlist) and
returns plain dict/bytes projections of the versioned REST responses; nothing
vendor-typed leaves this module's callers (ADR-0004 — the connector maps these
into domain values). There is no user-supplied URL anywhere: every request
targets ``www.googleapis.com`` paths built here, so a request leaving the
pinned host set is impossible by construction (and a defect the guard would
refuse anyway — ADR-0009 §3).

Fault posture:

* **429 / Retry-After** — bounded backoff (Drive's documented throttle), never
  in the request path (connectors run only inside the Celery sync task);
* **content is streamed under a hard byte cap** — export/download never
  materialize a response before checking it (an oversized ``Content-Length`` is
  refused up front and the body read stops at ``cap + 1`` bytes), so a missing
  or inaccurate Drive ``size`` cannot consume unbounded worker memory/egress;
* **HTTP 410** — the incremental cursor is expired: raised as the typed
  :class:`~app.connectors.base.CursorExpiredError` the framework maps to a
  cursor-clear + full resync (ADR-0019 §3);
* anything else non-2xx — a typed, vendor-free
  :class:`~app.connectors.base.ConnectorError`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.connectors.base import ConnectorError, CursorExpiredError

# The versioned REST base (pinned host: www.googleapis.com).
API_BASE = "https://www.googleapis.com/drive/v3"

# Bounded 429 handling: attempts and the ceiling on a server-suggested wait.
_MAX_ATTEMPTS = 3
_MAX_RETRY_AFTER_SECONDS = 30.0

# One page of files/changes/permissions per request (Drive's default max ballpark).
_PAGE_SIZE = 100

# Bytes per iteration step when streaming content — bounds how much of a body
# is materialized per loop, independent of the transport's own frame sizes.
_STREAM_CHUNK_BYTES = 64 * 1024

# The per-file fields every enumeration asks for (ADR-0019 §2: the effective
# permission list is fetched separately; the file carries the inheritance flag).
FILE_FIELDS = (
    "id,name,mimeType,size,modifiedTime,parents,driveId," "inheritedPermissionsDisabled,trashed"
)

# The §2 effective-read algorithm's requested permission-entry fields.
PERMISSION_FIELDS = "id,type,role,emailAddress,domain,deleted,expirationTime,view,permissionDetails"


def _retry_after_seconds(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After", "1")
    try:
        seconds = float(raw)
    except ValueError:
        seconds = 1.0
    return max(0.0, min(seconds, _MAX_RETRY_AFTER_SECONDS))


async def _request(
    http: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """One guarded GET with bounded 429 backoff; typed errors, never vendor ones."""
    last_status = 0
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = await http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise ConnectorError(
                f"drive api unreachable: {type(exc).__name__}", code="drive_unreachable"
            ) from exc
        if response.status_code == 429:
            last_status = 429
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_retry_after_seconds(response))
            continue
        if response.status_code == 410:
            raise CursorExpiredError("drive changes cursor expired (HTTP 410)")
        if response.status_code >= 400:
            # Status only — a Drive error body is vendor detail, never surfaced.
            raise ConnectorError(
                f"drive api returned {response.status_code}", code="drive_api_error"
            )
        return response
    raise ConnectorError(f"drive api returned {last_status}", code="drive_rate_limited")


async def _request_json(
    http: httpx.AsyncClient, url: str, *, params: dict[str, str] | None = None
) -> dict[str, Any]:
    response = await _request(http, url, params=params)
    try:
        payload = response.json()
    except ValueError as exc:
        raise ConnectorError("drive api returned a non-JSON body", code="drive_api_error") from exc
    if not isinstance(payload, dict):
        raise ConnectorError("drive api returned an unexpected shape", code="drive_api_error")
    return payload


async def get_start_page_token(http: httpx.AsyncClient, *, drive_id: str | None) -> str:
    """The change-log baseline, scoped per ADR-0019 §3.

    The user's own feed for My Drive / a My-Drive folder; the **Shared Drive's
    ``driveId``-scoped log** for a Shared Drive or any folder inside one —
    captured BEFORE enumeration so the first incremental replay covers the
    enumeration window.
    """
    params: dict[str, str] = {"supportsAllDrives": "true"}
    if drive_id is not None:
        params["driveId"] = drive_id
    payload = await _request_json(http, f"{API_BASE}/changes/startPageToken", params=params)
    token = payload.get("startPageToken")
    if not isinstance(token, str) or not token:
        raise ConnectorError("drive api returned no startPageToken", code="drive_api_error")
    return token


async def list_files(
    http: httpx.AsyncClient,
    *,
    query: str,
    drive_id: str | None,
) -> list[dict[str, Any]]:
    """Every file matching ``query`` (paginated ``files.list``), vendor dicts."""
    files: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params: dict[str, str] = {
            "q": query,
            "pageSize": str(_PAGE_SIZE),
            "fields": f"nextPageToken,files({FILE_FIELDS})",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if drive_id is not None:
            params["corpora"] = "drive"
            params["driveId"] = drive_id
        if page_token is not None:
            params["pageToken"] = page_token
        payload = await _request_json(http, f"{API_BASE}/files", params=params)
        batch = payload.get("files")
        if isinstance(batch, list):
            files.extend(entry for entry in batch if isinstance(entry, dict))
        token = payload.get("nextPageToken")
        if not isinstance(token, str) or not token:
            return files
        page_token = token


async def get_file(http: httpx.AsyncClient, file_id: str) -> dict[str, Any]:
    """One file's metadata (``files.get`` with the standard field set)."""
    return await _request_json(
        http,
        f"{API_BASE}/files/{file_id}",
        params={"fields": FILE_FIELDS, "supportsAllDrives": "true"},
    )


async def list_permissions(http: httpx.AsyncClient, file_id: str) -> list[dict[str, Any]]:
    """The file's own effective permission list (paginated), vendor dicts."""
    permissions: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params: dict[str, str] = {
            "pageSize": str(_PAGE_SIZE),
            "fields": f"nextPageToken,permissions({PERMISSION_FIELDS})",
            "supportsAllDrives": "true",
        }
        if page_token is not None:
            params["pageToken"] = page_token
        payload = await _request_json(
            http, f"{API_BASE}/files/{file_id}/permissions", params=params
        )
        batch = payload.get("permissions")
        if isinstance(batch, list):
            permissions.extend(entry for entry in batch if isinstance(entry, dict))
        token = payload.get("nextPageToken")
        if not isinstance(token, str) or not token:
            return permissions
        page_token = token


def _append_capped(buffer: bytearray, chunk: bytes, max_bytes: int) -> bool:
    """Append at most ``max_bytes + 1 - len(buffer)`` bytes; ``True`` = over cap.

    The cap has to bound **memory**, not just the return value, so the chunk is
    *sliced* rather than appended whole: a single large transport frame — or a
    large decoded frame from a compressed response — would otherwise land in the
    accumulator in full before the size test ran, and the bound would be a
    fiction. ``cap + 1`` bytes is the most this buffer can ever hold: exactly
    enough to prove the body exceeds the cap, and never more.
    """
    room = max_bytes + 1 - len(buffer)
    if room > 0:
        buffer.extend(chunk[:room])
    return len(buffer) > max_bytes


def _declared_over_cap(response: httpx.Response, max_bytes: int) -> bool:
    """True iff the response *declares* a body larger than the cap."""
    raw = response.headers.get("Content-Length")
    if raw is None:
        return False
    try:
        return int(raw) > max_bytes
    except ValueError:
        return False


async def _stream_capped(
    http: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str],
    max_bytes: int,
) -> bytes | None:
    """Stream one guarded GET, bounded to ``max_bytes``; ``None`` = over the cap.

    The content path is the only place a Drive response can be arbitrarily
    large, so it is **streamed** rather than buffered by ``response.content``:

    * an oversized declared ``Content-Length`` is rejected **before** the body
      is read at all (no egress, no memory);
    * otherwise the body is consumed in bounded steps and reading **stops** as
      soon as ``max_bytes`` is exceeded, with each step appended through
      :func:`_append_capped` so the accumulator itself never exceeds
      ``cap + 1`` bytes — a missing or lying ``size`` on the file metadata, a
      huge single frame, or a compressed body that decodes large can therefore
      never make a worker buffer an unbounded response (the connection is
      dropped by exiting the stream context).

    Returns ``None`` for both over-cap outcomes so the caller can skip-and-count
    the file (ADR-0019 §5), and the same bounded 429/410/error posture as
    :func:`_request` otherwise.
    """
    last_status = 0
    for attempt in range(_MAX_ATTEMPTS):
        try:
            async with http.stream("GET", url, params=params) as response:
                if response.status_code == 429:
                    last_status = 429
                    if attempt < _MAX_ATTEMPTS - 1:
                        await asyncio.sleep(_retry_after_seconds(response))
                    continue
                if response.status_code == 410:
                    raise CursorExpiredError("drive changes cursor expired (HTTP 410)")
                if response.status_code >= 400:
                    raise ConnectorError(
                        f"drive api returned {response.status_code}", code="drive_api_error"
                    )
                if _declared_over_cap(response, max_bytes):
                    return None
                buffer = bytearray()
                # Bounded iteration size AND a bounded append. The step size is
                # capped by the *budget* as well as by the ceiling: httpx's
                # chunker accumulates up to `chunk_size` before it yields, so
                # asking for 64KB when the budget is 32 bytes would pull far
                # more of the body than the decision needs. `_append_capped`
                # then slices whatever still arrives, so neither a large
                # transport frame nor a large decoded frame can grow the
                # accumulator past cap + 1.
                step = min(_STREAM_CHUNK_BYTES, max_bytes + 1)
                async for chunk in response.aiter_bytes(chunk_size=step):
                    if _append_capped(buffer, chunk, max_bytes):
                        return None
                return bytes(buffer)
        except httpx.HTTPError as exc:
            raise ConnectorError(
                f"drive api unreachable: {type(exc).__name__}", code="drive_unreachable"
            ) from exc
    raise ConnectorError(f"drive api returned {last_status}", code="drive_rate_limited")


async def export_file(
    http: httpx.AsyncClient, file_id: str, *, mime_type: str, max_bytes: int
) -> bytes | None:
    """Export a Google-native file (Docs→text, Sheets→CSV, Slides→text).

    Streamed under ``max_bytes``; ``None`` = the export exceeds the cap (an
    export has no declared size upfront, which is exactly why it is streamed).
    """
    return await _stream_capped(
        http,
        f"{API_BASE}/files/{file_id}/export",
        params={"mimeType": mime_type},
        max_bytes=max_bytes,
    )


async def download_file(http: httpx.AsyncClient, file_id: str, *, max_bytes: int) -> bytes | None:
    """Download a binary file's bytes (``alt=media``), capped; ``None`` = over cap."""
    return await _stream_capped(
        http,
        f"{API_BASE}/files/{file_id}",
        params={"alt": "media", "supportsAllDrives": "true"},
        max_bytes=max_bytes,
    )


async def list_changes_page(
    http: httpx.AsyncClient,
    *,
    page_token: str,
    drive_id: str | None,
) -> dict[str, Any]:
    """One ``changes.list`` page (drive-scoped when the source is; ADR-0019 §3).

    Raises :class:`CursorExpiredError` on HTTP 410 (expired token) — the
    framework's cursor-clear + full-resync signal.
    """
    params: dict[str, str] = {
        "pageToken": page_token,
        "pageSize": str(_PAGE_SIZE),
        "fields": (
            "nextPageToken,newStartPageToken,"
            f"changes(changeType,removed,fileId,driveId,file({FILE_FIELDS}))"
        ),
        "includeRemoved": "true",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    if drive_id is not None:
        params["driveId"] = drive_id
    return await _request_json(http, f"{API_BASE}/changes", params=params)


async def fetch_about_email(http: httpx.AsyncClient) -> str | None:
    """The authenticated account's email (``about.get``), or ``None``."""
    payload = await _request_json(
        http, f"{API_BASE}/about", params={"fields": "user(emailAddress)"}
    )
    user = payload.get("user")
    if not isinstance(user, dict):
        return None
    email = user.get("emailAddress")
    return email if isinstance(email, str) and email else None
