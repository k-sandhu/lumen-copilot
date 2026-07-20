"""The Google Drive connector — read-only sync + ACL mirror + incremental cursor.

F-CB-2 (#453), ADR-0019 §2/§3/§5. The single module boundary that talks to
Google (ADR-0004): plain httpx against Drive REST v3 over the framework's
guarded, authenticated ``run.http`` — no Google SDK, no vault/DB/config-secret
access from connector code (the ADR-0019 §4 prohibitions; ``oauth_spec()`` may
read deployment config for the platform's client registration, exactly like
the framework expects).

Capabilities (auto-discovered off this object, ADR-0019 §4):

* ``oauth_spec()`` — the fixed Google endpoints + the pinned host set;
* ``fetch_account_email(http)`` — the post-exchange identity probe
  (``about.get``; the ``drive.readonly`` exchange carries no identity claim);
* ``sync(source, run)`` — full enumeration per mode, **start token captured
  before enumeration** (returned via :class:`FullSyncResult` so the framework
  stores it — the first incremental replay covers the enumeration window);
* ``fetch_changes(source, cursor, run)`` — the §3 page-shaped replay
  (drive-scoped log for Shared-Drive modes; removals → deletions; container
  changes → ``stale_scope_ids`` + in-run descendant re-examination rooted at
  the configured scope; **every live change proved to sit inside the configured
  subtree before import**, a move out reconciled as a deletion; unprovable
  effects → ``integrity=incomplete``; HTTP 410 → typed cursor-expired);
* ``map_acl(raw, ctx)`` — the pure §2 effective-read mapper
  (:mod:`app.connectors.gdrive.acl`).

Content (§5): Google-native exports (Docs → text/plain, Sheets → text/csv
flattened to text, Slides → text/plain) plus pass-through of the binary types
the ingestion pipeline already parses, within ``GDRIVE_FETCH_MAX_BYTES``;
everything else is skipped and counted. Per-file ``acl_scope_ids`` carry the
drive id + ancestor folder ids so the framework's cascade stale-stamp can
match descendants (§3).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.connectors.base import (
    AclMappingContext,
    ConnectorConfigError,
    ConnectorError,
    ConnectorHealth,
    ConnectorRun,
    FetchedDoc,
    FullSyncResult,
    PageIntegrity,
    SourceAcl,
    SyncPage,
)
from app.connectors.gdrive import api
from app.connectors.gdrive.acl import map_acl as _map_acl_impl
from app.connectors.oauth import OAuthSpec
from app.core.logging import get_logger
from app.domain.entities import Source

log = get_logger(__name__)

_FOLDER_MIME = "application/vnd.google-apps.folder"

# Google-native types → the export MIME requested from Drive (ADR-0019 §5).
# Sheets export as CSV but are *stored* as text/plain — the exported CSV is
# plain text the ingestion pipeline chunks directly (multi-sheet fidelity is
# the recorded CSV-flatten limitation, fenced out of v1).
_EXPORT_MIME = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

# Binary types passed through to the existing ingestion parsers (#21).
_PASSTHROUGH_MIMES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/plain",
        "text/markdown",
    }
)

_MODES = ("my_drive", "folder", "shared_drive")

# Bounded permission-fetch retry per document (ADR-0019 §2/§3: skip + no
# freshness advance on persistent failure; the 429 backoff lives one level
# down in the api helper).
_ACL_FETCH_ATTEMPTS = 2

# Cascade re-examination budget per incremental run (ADR-0019 §3): more
# affected descendants than this ⇒ the connector cannot re-examine the set in
# this run ⇒ integrity=incomplete (the framework fails closed source-wide).
_CASCADE_REFETCH_BUDGET = 200

# Upper bound on ancestor-chain walks (cycle/defect guard).
_MAX_ANCESTOR_DEPTH = 64

# Per-run memo of ``file id → parent id`` (``None`` = no/unreadable parent), so
# the subtree proofs a change page needs cost one lookup per distinct ancestor.
_ParentCache = dict[str, "str | None"]


@dataclass(frozen=True, slots=True)
class _ModeConfig:
    """The validated ``{mode, folder_id?, drive_id?}`` triple (ADR-0019 §5)."""

    mode: str
    folder_id: str | None
    drive_id: str | None

    @property
    def change_log_drive_id(self) -> str | None:
        """The §3 change-log scope: the Shared Drive's id for a Shared Drive
        *or any folder inside one*; ``None`` = the user's own feed."""
        return self.drive_id


def _mode_config(config: Mapping[str, object]) -> _ModeConfig:
    """Parse a stored source config into the mode triple (fail closed)."""
    mode = config.get("mode")
    folder_id = config.get("folder_id")
    drive_id = config.get("drive_id")
    if mode not in _MODES:
        raise ConnectorConfigError(f"unknown gdrive mode {mode!r}")
    return _ModeConfig(
        mode=str(mode),
        folder_id=str(folder_id) if isinstance(folder_id, str) and folder_id else None,
        drive_id=str(drive_id) if isinstance(drive_id, str) and drive_id else None,
    )


class _AclUnavailable(Exception):
    """A file's effective permissions could not be read this run (§2).

    Module-private control flow, never surfaced: it separates "we do not know
    this file's current rights" (skip without advancing freshness; an
    incremental page over it is ``integrity=incomplete``) from "this file is
    not something we mirror at all" (``_fetch_doc`` → ``None``, reconciled as a
    deletion). Conflating the two is how an unreadable ACL used to look like a
    type we never ingest.
    """


def _parse_modified(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GdriveConnector:
    """The managed Google Drive connector (ADR-0019 §5)."""

    name = "gdrive"

    # --- config + OAuth capability ------------------------------------------

    def validate_config(self, config: dict[str, object]) -> dict[str, object]:
        """Validate the closed mode variants (INV-8; contract GdriveSourceConfig).

        ``my_drive`` takes no ids; ``folder`` requires ``folder_id`` (optional
        ``drive_id`` when the folder lives in a Shared Drive); ``shared_drive``
        requires ``drive_id`` and forbids ``folder_id``. Anything else is a
        typed ``invalid_config`` → 422.
        """
        mode = config.get("mode")
        folder_id = config.get("folder_id")
        drive_id = config.get("drive_id")
        if mode not in _MODES:
            raise ConnectorConfigError(f"gdrive mode must be one of {_MODES}")
        if mode == "my_drive":
            if folder_id is not None or drive_id is not None:
                raise ConnectorConfigError("my_drive takes no folder_id/drive_id")
            return {"mode": "my_drive"}
        if mode == "folder":
            if not isinstance(folder_id, str) or not folder_id:
                raise ConnectorConfigError("folder mode requires folder_id")
            if drive_id is not None and (not isinstance(drive_id, str) or not drive_id):
                raise ConnectorConfigError("drive_id must be a non-empty string")
            validated: dict[str, object] = {"mode": "folder", "folder_id": folder_id}
            if isinstance(drive_id, str) and drive_id:
                validated["drive_id"] = drive_id
            return validated
        # shared_drive
        if folder_id is not None:
            raise ConnectorConfigError("shared_drive mode takes no folder_id")
        if not isinstance(drive_id, str) or not drive_id:
            raise ConnectorConfigError("shared_drive mode requires drive_id")
        return {"mode": "shared_drive", "drive_id": drive_id}

    def oauth_spec(self) -> OAuthSpec:
        """Google's fixed OAuth endpoints + the pinned host set (ADR-0019 §5).

        The client registration is deployment-level config (``core/config`` —
        the one non-secret config surface a connector may read, exactly as the
        framework's OAuth flow expects). ``drive.readonly`` covers content +
        metadata + per-file permissions; ``access_type=offline&prompt=consent``
        guarantees a refresh token on every (re)connect.
        """
        from app.core.config import get_settings

        settings = get_settings()
        return OAuthSpec(
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=("https://www.googleapis.com/auth/drive.readonly",),
            client_id=settings.gdrive_oauth_client_id,
            client_secret=settings.gdrive_oauth_client_secret,
            allowed_hosts=(
                "accounts.google.com",
                "oauth2.googleapis.com",
                "www.googleapis.com",
            ),
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
        )

    async def fetch_account_email(self, http: httpx.AsyncClient) -> str | None:
        """The provider-verified account identity (ADR-0019 §1: ``about.get``).

        Runs over the framework-built guarded client right after the code
        exchange — the ``drive.readonly`` exchange itself returns no identity
        claim. A probe failure is the caller's ``None`` (never a crash).
        """
        return await api.fetch_about_email(http)

    # --- ACL capability (pure) ----------------------------------------------

    def map_acl(self, raw: Mapping[str, object], ctx: AclMappingContext) -> frozenset[str]:
        """The §2 effective-read mapper — pure, fail-closed (see ``acl.py``)."""
        return _map_acl_impl(raw, ctx)

    # --- health --------------------------------------------------------------

    async def health(self, source: Source, run: ConnectorRun) -> ConnectorHealth:
        """A bounded token-validity probe (``about.get``), ADR-0019 §5."""
        try:
            email = await api.fetch_about_email(run.http)
        except Exception as exc:  # noqa: BLE001 — a probe fault is a health result
            return ConnectorHealth(healthy=False, detail=type(exc).__name__)
        return ConnectorHealth(healthy=True, detail=email)

    # --- full sync ------------------------------------------------------------

    async def sync(self, source: Source, run: ConnectorRun) -> FullSyncResult:
        """Full enumeration per mode (ADR-0019 §3 bootstrap).

        The change-log start token is captured **before** enumeration begins,
        scoped to the right log for the mode, and returned as the
        ``baseline_cursor`` — nothing that changes mid-enumeration is lost.
        """
        from app.core.config import get_settings

        cap = get_settings().gdrive_fetch_max_bytes
        cfg = _mode_config(source.config)
        ctx = run.acl_context

        # 1. Start token FIRST (drive-scoped for Shared-Drive modes).
        baseline = await api.get_start_page_token(run.http, drive_id=cfg.change_log_drive_id)

        # 2. Enumerate files + folder topology per mode.
        files, chains = await self._enumerate(run.http, cfg)

        # 3. Fetch permissions + content per file (fail-closed skips).
        docs: list[FetchedDoc] = []
        skipped = 0
        for file in files:
            try:
                doc = await self._fetch_doc(
                    run.http,
                    file,
                    scope_chain=chains.get(str(file.get("id")), ()),
                    ctx=ctx,
                    cap=cap,
                )
            except _AclUnavailable:
                # Unknown rights ⇒ skipped this sync (never ingested), counted.
                skipped += 1
                continue
            if doc is None:
                skipped += 1
                continue
            docs.append(doc)
        return FullSyncResult(docs=tuple(docs), baseline_cursor=baseline, skipped_count=skipped)

    async def _enumerate(
        self, http: httpx.AsyncClient, cfg: _ModeConfig
    ) -> tuple[list[dict[str, Any]], dict[str, tuple[str, ...]]]:
        """Enumerate non-folder files + each file's container scope chain.

        Returns ``(files, {file_id: scope_chain})`` where the chain is the
        drive id (when any) + the ancestor folder ids known from the
        enumeration topology — persisted as ``acl_scope_ids`` so a replayed
        container permission change can stale-stamp descendants (§3).
        """
        root_chain: tuple[str, ...] = ()
        if cfg.drive_id is not None:
            root_chain = (cfg.drive_id,)

        if cfg.mode == "folder":
            assert cfg.folder_id is not None  # validated shape  # noqa: S101
            # Watch the ancestors ABOVE the sync root too: a permission change
            # to one of them cascades over everything we sync.
            uppers = await self._walk_up(http, cfg.folder_id)
            root_chain = root_chain + uppers
            return await self._enumerate_folder_tree(
                http, cfg.folder_id, drive_id=cfg.drive_id, root_chain=root_chain
            )

        query = "trashed = false"
        entries = await api.list_files(http, query=query, drive_id=cfg.drive_id)
        folders = {
            str(e["id"]): e for e in entries if e.get("mimeType") == _FOLDER_MIME and "id" in e
        }
        files = [e for e in entries if e.get("mimeType") != _FOLDER_MIME]

        def chain_of(entry: dict[str, Any]) -> tuple[str, ...]:
            chain: list[str] = list(root_chain)
            drive_id = entry.get("driveId")
            if isinstance(drive_id, str) and drive_id and drive_id not in chain:
                chain.append(drive_id)
            parents = entry.get("parents")
            current = parents[0] if isinstance(parents, list) and parents else None
            depth = 0
            while isinstance(current, str) and depth < _MAX_ANCESTOR_DEPTH:
                if current in chain:
                    break
                chain.append(current)
                parent_entry = folders.get(current)
                if parent_entry is None:
                    break
                grand = parent_entry.get("parents")
                current = grand[0] if isinstance(grand, list) and grand else None
                depth += 1
            return tuple(chain)

        chains = {str(f["id"]): chain_of(f) for f in files if "id" in f}
        return files, chains

    async def _enumerate_folder_tree(
        self,
        http: httpx.AsyncClient,
        root_folder_id: str,
        *,
        drive_id: str | None,
        root_chain: tuple[str, ...],
    ) -> tuple[list[dict[str, Any]], dict[str, tuple[str, ...]]]:
        """BFS a folder subtree; each file's chain is its ancestor path."""
        files: list[dict[str, Any]] = []
        chains: dict[str, tuple[str, ...]] = {}
        queue: list[tuple[str, tuple[str, ...]]] = [
            (root_folder_id, root_chain + (root_folder_id,))
        ]
        seen: set[str] = {root_folder_id}
        while queue:
            folder_id, chain = queue.pop(0)
            entries = await api.list_files(
                http, query=f"'{folder_id}' in parents and trashed = false", drive_id=drive_id
            )
            for entry in entries:
                entry_id = entry.get("id")
                if not isinstance(entry_id, str):
                    continue
                if entry.get("mimeType") == _FOLDER_MIME:
                    if entry_id not in seen and len(chain) < _MAX_ANCESTOR_DEPTH:
                        seen.add(entry_id)
                        queue.append((entry_id, chain + (entry_id,)))
                    continue
                files.append(entry)
                chains[entry_id] = chain
        return files, chains

    async def _walk_up(
        self, http: httpx.AsyncClient, start_id: str, *, cache: _ParentCache | None = None
    ) -> tuple[str, ...]:
        """The ancestor ids ABOVE a container (``files.get`` walk, bounded).

        Drive files have a single parent (multi-parenting was removed in 2020),
        so ``parents[0]`` is *the* position — the walk is a chain, not a graph.
        ``cache`` memoizes ``child → parent`` for one run so a change page full
        of siblings costs one lookup per distinct ancestor, not per file.
        """
        parents = cache if cache is not None else {}
        ancestors: list[str] = []
        current = start_id
        for _ in range(_MAX_ANCESTOR_DEPTH):
            parent = await self._parent_of(http, current, cache=parents)
            if parent is None or parent in ancestors:
                # The top of a chain may be unreadable (e.g. the My Drive
                # root); the ids collected so far still widen the watch set.
                break
            ancestors.append(parent)
            current = parent
        return tuple(ancestors)

    async def _parent_of(
        self, http: httpx.AsyncClient, file_id: str, *, cache: _ParentCache
    ) -> str | None:
        """The file's parent id (memoized); ``None`` = no/unreadable parent."""
        if file_id in cache:
            return cache[file_id]
        try:
            entry = await api.get_file(http, file_id)
        except ConnectorError:
            cache[file_id] = None
            return None
        raw = entry.get("parents")
        parent = raw[0] if isinstance(raw, list) and raw else None
        resolved = parent if isinstance(parent, str) and parent else None
        cache[file_id] = resolved
        return resolved

    async def _is_within_root(
        self,
        http: httpx.AsyncClient,
        file: Mapping[str, Any],
        cfg: _ModeConfig,
        *,
        cache: _ParentCache,
    ) -> bool:
        """Is this file **currently** inside the configured subtree? (§5 scope).

        Only ``folder`` mode has a subtree narrower than its change log: the
        Drive changes feed is per-user (or per-Shared-Drive) and reports every
        file the account can see, so an incremental replay MUST prove a changed
        file's current ancestor chain still contains the configured
        ``folder_id`` before importing it. A file whose position cannot be
        established (no readable parent) counts as **outside** — the connector
        never imports content it cannot place inside the configured scope.
        """
        if cfg.mode != "folder" or cfg.folder_id is None:
            return True  # my_drive / shared_drive: the whole log IS the scope
        raw = file.get("parents")
        parent = raw[0] if isinstance(raw, list) and raw else None
        if not isinstance(parent, str) or not parent:
            return False
        if parent == cfg.folder_id:
            return True
        return cfg.folder_id in await self._walk_up(http, parent, cache=cache)

    async def _container_relation(
        self,
        http: httpx.AsyncClient,
        container_id: str,
        cfg: _ModeConfig,
        *,
        cache: _ParentCache,
    ) -> str:
        """How a changed container relates to the configured root (§3 cascade).

        * ``"inside"`` — the root itself or a folder within it: its permission
          change cascades over a *subset* of the corpus, so re-examination
          starts at that folder;
        * ``"above"`` — an ancestor of the configured root (or the enclosing
          Shared Drive): its change cascades over **everything** we sync, so
          re-examination starts at the configured root — never at the
          ancestor's own subtree, which would enumerate siblings the source was
          never configured to ingest;
        * ``"outside"`` — unrelated to the configured subtree: ignored.
        """
        if cfg.mode != "folder" or cfg.folder_id is None:
            return "inside"  # the whole drive/feed is the configured scope
        if container_id == cfg.folder_id:
            return "inside"
        if cfg.folder_id in await self._walk_up(http, container_id, cache=cache):
            return "inside"
        if container_id in await self._walk_up(http, cfg.folder_id, cache=cache):
            return "above"
        return "outside"

    async def _fetch_doc(
        self,
        http: httpx.AsyncClient,
        file: Mapping[str, Any],
        *,
        scope_chain: tuple[str, ...],
        ctx: AclMappingContext | None,
        cap: int,
    ) -> FetchedDoc | None:
        """One file → a :class:`FetchedDoc`, or ``None`` = **not ingestible**.

        ``None`` means the file is outside what this connector mirrors at all
        (unsupported type, or over the size cap) — a *stable* property of the
        file, which an incremental replay reconciles as a deletion.

        A permissions fetch that keeps failing is a different outcome and
        raises :class:`_AclUnavailable`: the file *is* ingestible, we simply do
        not know its current rights, so it must be skipped without advancing
        ``acl_synced_at`` (ADR-0019 §2 — never ingested/kept with unknown
        rights) and an incremental page must fail closed over it.
        """
        file_id = file.get("id")
        name = file.get("name")
        mime = file.get("mimeType")
        if not isinstance(file_id, str) or not isinstance(mime, str):
            return None
        title = name if isinstance(name, str) and name else file_id

        # --- content ---------------------------------------------------------
        export_mime = _EXPORT_MIME.get(mime)
        text = ""
        data: bytes | None = None
        stored_mime = "text/plain"
        if export_mime is not None:
            # Streamed under the cap inside the api helper — an over-cap export
            # answers ``None`` without ever being buffered whole.
            payload = await api.export_file(http, file_id, mime_type=export_mime, max_bytes=cap)
            if payload is None:
                log.info("gdrive.skip_oversize", file_id=file_id)
                return None
            text = payload.decode("utf-8", errors="replace")
        elif mime in _PASSTHROUGH_MIMES:
            declared = file.get("size")
            declared_bytes = (
                int(declared) if isinstance(declared, str) and declared.isdigit() else None
            )
            if declared_bytes is not None and declared_bytes > cap:
                # Cheap pre-check off the metadata: skip without any transfer.
                log.info("gdrive.skip_oversize", file_id=file_id, size=declared_bytes)
                return None
            payload = await api.download_file(http, file_id, max_bytes=cap)
            if payload is None:
                # A missing/lying ``size`` is caught by the streamed cap.
                log.info("gdrive.skip_oversize", file_id=file_id)
                return None
            data = payload
            stored_mime = mime
        else:
            log.info("gdrive.skip_unsupported", file_id=file_id, mime=mime)
            return None

        # --- effective permissions (bounded retry; fail closed on exhaustion)
        permissions = await self._fetch_permissions(http, file_id)
        if permissions is None:
            raise _AclUnavailable(file_id)
        raw_acl: dict[str, object] = {
            "permissions": permissions,
            "inheritedPermissionsDisabled": file.get("inheritedPermissionsDisabled") is True,
        }
        effective_ctx = ctx if ctx is not None else _EMPTY_CTX
        principals = self.map_acl(raw_acl, effective_ctx)

        scope_ids = set(scope_chain)
        drive_id = file.get("driveId")
        if isinstance(drive_id, str) and drive_id:
            scope_ids.add(drive_id)

        return FetchedDoc(
            title=title,
            text=text,
            url=f"https://drive.google.com/open?id={file_id}",
            external_id=file_id,
            modified_at=_parse_modified(file.get("modifiedTime")),
            acl=SourceAcl(principals=principals, scope_ids=frozenset(scope_ids)),
            data=data,
            mime_type=stored_mime,
        )

    async def _fetch_permissions(
        self, http: httpx.AsyncClient, file_id: str
    ) -> list[dict[str, Any]] | None:
        """The file's permission list with a bounded retry; ``None`` = give up.

        A ``None`` means the caller must skip the document this sync — its
        ``acl_synced_at`` never advances on unknown rights (ADR-0019 §2).
        """
        for attempt in range(_ACL_FETCH_ATTEMPTS):
            try:
                return await api.list_permissions(http, file_id)
            except ConnectorError as exc:
                if attempt == _ACL_FETCH_ATTEMPTS - 1:
                    log.warning("gdrive.acl_fetch_failed", file_id=file_id, code=exc.code)
                    return None
        return None  # pragma: no cover — loop always returns

    # --- incremental sync -----------------------------------------------------

    async def fetch_changes(
        self, source: Source, cursor: str, run: ConnectorRun
    ) -> AsyncIterator[SyncPage]:
        """Replay the change log from ``cursor`` as §3 :class:`SyncPage`\\ s.

        Per page: removals/trash → ``deleted_external_ids``; a change to a
        cascading container (a folder, or the Shared Drive itself) → its id in
        ``stale_scope_ids`` **plus** an in-run re-examination of its
        descendants (budget-bounded); a change whose effects cannot be proven
        complete (missing file metadata, enumeration failure, budget/ACL-fetch
        exhaustion) → ``integrity=incomplete`` (the framework fails closed
        source-wide). The terminal page's ``next_cursor`` is Drive's
        ``newStartPageToken`` — the new baseline. HTTP 410 raises the typed
        cursor-expired error before any page is yielded for that token.

        **The change log is wider than the configured scope.** Drive's feed is
        per-user (or per-Shared-Drive); in ``folder`` mode it therefore reports
        files the source was never configured to ingest. Every live change is
        proved to sit inside the configured subtree *at its current position*
        before it is upserted, and a file that moved **out** (or stopped being
        ingestible) is reconciled as a **deletion** — the framework's
        identity-delete is a no-op when no row exists, so this is safe for
        files that were never ours. Cascade re-examination likewise enumerates
        the **configured root**, never an arbitrary ancestor scope whose
        subtree contains siblings outside the source.
        """
        from app.core.config import get_settings

        cap = get_settings().gdrive_fetch_max_bytes
        cfg = _mode_config(source.config)
        ctx = run.acl_context
        token = cursor
        refetch_budget = _CASCADE_REFETCH_BUDGET
        parents: _ParentCache = {}
        while True:
            payload = await api.list_changes_page(
                run.http, page_token=token, drive_id=cfg.change_log_drive_id
            )
            upserts: list[FetchedDoc] = []
            deleted: set[str] = set()
            stale_scopes: set[str] = set()
            # Where re-examination must START for this page's cascades — the
            # changed container itself when it is inside the configured
            # subtree, the configured root when the change is above it.
            refetch_roots: set[str] = set()
            incomplete = False

            changes = payload.get("changes")
            for change in changes if isinstance(changes, list) else []:
                if not isinstance(change, dict):
                    incomplete = True
                    continue
                if change.get("changeType") == "drive":
                    drive_id = change.get("driveId")
                    if not (isinstance(drive_id, str) and drive_id):
                        incomplete = True
                        continue
                    # A Shared-Drive permission change cascades over everything
                    # we sync — but re-examination starts at the configured
                    # root, which in folder mode is narrower than the drive.
                    stale_scopes.add(drive_id)
                    refetch_roots.add(cfg.folder_id or drive_id)
                    continue
                file_id = change.get("fileId")
                if not isinstance(file_id, str) or not file_id:
                    incomplete = True
                    continue
                file = change.get("file")
                if change.get("removed") or (
                    isinstance(file, dict) and file.get("trashed") is True
                ):
                    deleted.add(file_id)
                    continue
                if not isinstance(file, dict):
                    # A live change without file metadata: its effect on the
                    # mirror is unprovable — fail closed.
                    incomplete = True
                    continue
                if file.get("mimeType") == _FOLDER_MIME:
                    # A container change cascades (Drive emits no per-
                    # descendant events): signal the scope for the framework's
                    # immediate stale-stamp, then re-examine descendants below.
                    relation = await self._container_relation(run.http, file_id, cfg, cache=parents)
                    if relation == "outside":
                        continue  # a container we do not sync — not our cascade
                    stale_scopes.add(file_id)
                    refetch_roots.add(file_id if relation == "inside" else str(cfg.folder_id))
                    continue
                if not await self._is_within_root(run.http, file, cfg, cache=parents):
                    # Outside the configured subtree *now*: never import it, and
                    # reconcile away a row left behind by a move OUT of scope.
                    deleted.add(file_id)
                    continue
                chain = await self._change_scope_chain(run.http, file, cfg, cache=parents)
                try:
                    doc = await self._fetch_doc(run.http, file, scope_chain=chain, ctx=ctx, cap=cap)
                except _AclUnavailable:
                    # Ingestible, but its current rights are unknown — the
                    # page's effect on the mirror is unprovable (fail closed).
                    incomplete = True
                    continue
                if doc is None:
                    # No longer ingestible (unsupported type / over the cap):
                    # a stable property, reconciled as a deletion so the mirror
                    # never keeps content we can no longer refresh.
                    deleted.add(file_id)
                    continue
                upserts.append(doc)

            # Re-examine the descendants of every cascade root in-run (§3: the
            # stamp denies them immediately; these upserts restore freshness
            # per document as each is re-examined).
            for root_id in sorted(refetch_roots):
                refetched, used, complete = await self._refetch_scope(
                    run.http, root_id, cfg=cfg, ctx=ctx, cap=cap, budget=refetch_budget
                )
                refetch_budget -= used
                upserts.extend(refetched)
                if not complete:
                    incomplete = True

            next_token = payload.get("nextPageToken")
            new_baseline = payload.get("newStartPageToken")
            terminal = not (isinstance(next_token, str) and next_token)
            if terminal:
                if not (isinstance(new_baseline, str) and new_baseline):
                    raise ConnectorError(
                        "drive returned no newStartPageToken", code="drive_api_error"
                    )
                next_cursor = new_baseline
            else:
                next_cursor = next_token  # type: ignore[assignment]

            yield SyncPage(
                upserts=tuple(upserts),
                deleted_external_ids=frozenset(deleted),
                next_cursor=str(next_cursor),
                stale_scope_ids=frozenset(stale_scopes),
                integrity=PageIntegrity.INCOMPLETE if incomplete else PageIntegrity.COMPLETE,
            )
            if terminal:
                return
            token = str(next_token)

    @staticmethod
    def _is_ingestible(file: Mapping[str, Any]) -> bool:
        mime = file.get("mimeType")
        return isinstance(mime, str) and (mime in _EXPORT_MIME or mime in _PASSTHROUGH_MIMES)

    async def _change_scope_chain(
        self,
        http: httpx.AsyncClient,
        file: Mapping[str, Any],
        cfg: _ModeConfig,
        *,
        cache: _ParentCache | None = None,
    ) -> tuple[str, ...]:
        """A changed file's scope chain: drive id + upward ancestor walk."""
        chain: list[str] = []
        if cfg.drive_id is not None:
            chain.append(cfg.drive_id)
        drive_id = file.get("driveId")
        if isinstance(drive_id, str) and drive_id and drive_id not in chain:
            chain.append(drive_id)
        parents = file.get("parents")
        parent = parents[0] if isinstance(parents, list) and parents else None
        if isinstance(parent, str):
            chain.append(parent)
            try:
                uppers = await self._walk_up(http, parent, cache=cache)
            except ConnectorError:
                uppers = ()
            chain.extend(a for a in uppers if a not in chain)
        return tuple(chain)

    async def _refetch_scope(
        self,
        http: httpx.AsyncClient,
        root_id: str,
        *,
        cfg: _ModeConfig,
        ctx: AclMappingContext | None,
        cap: int,
        budget: int,
    ) -> tuple[list[FetchedDoc], int, bool]:
        """Re-examine a cascade root's descendants (budget-bounded).

        ``root_id`` is always a container **inside the configured scope** (the
        caller maps an ancestor/drive change onto the configured root first),
        so this enumeration can never reach siblings outside the source.

        Returns ``(docs, files_examined, complete)`` — ``complete=False`` when
        the walk failed, the budget ran out, or a descendant's rights could not
        be re-read (each of them the §3 unprovable-set signal).
        """
        docs: list[FetchedDoc] = []
        used = 0
        try:
            files, chains = await self._enumerate_folder_tree(
                http,
                root_id,
                drive_id=cfg.change_log_drive_id,
                root_chain=(cfg.drive_id,) if cfg.drive_id is not None else (),
            )
        except ConnectorError:
            # Enumeration failure: descendants stay stale-stamped (denied
            # immediately) and the set is unprovable.
            return [], used, False
        for file in files:
            if used >= budget:
                return docs, used, False
            used += 1
            try:
                doc = await self._fetch_doc(
                    http,
                    file,
                    scope_chain=chains.get(str(file.get("id")), ()),
                    ctx=ctx,
                    cap=cap,
                )
            except _AclUnavailable:
                return docs, used, False
            if doc is None:
                continue  # not ingestible at all — nothing to re-attest
            docs.append(doc)
        return docs, used, True


# The fail-closed empty identity snapshot (used only if the framework did not
# supply one — attested-user mapping then admits nobody; `anyone` → tenant
# still holds because it needs no identity map).
_EMPTY_CTX = AclMappingContext(
    email_to_user_id={},
    evaluated_at=datetime.fromtimestamp(0, tz=UTC),
)

__all__ = ["GdriveConnector"]
